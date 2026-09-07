"""数据源服务：连接测试、Schema 采集（表/字段 comment、主键/唯一索引、外键关系图信息）。"""
import logging
from datetime import datetime

from sqlalchemy import create_engine, text
from ..database import SessionLocal
from ..models import ColumnMeta, Datasource, Relationship, TableMeta

# AI_Infra 系统表黑名单：同步业务数据源 Schema 时跳过，避免混入元数据
_SYSTEM_TABLE_BLACKLIST = frozenset({
    "column_meta", "table_meta", "datasource", "permission_rule", "users", "user", "role",
    "conversation", "conversation_message", "query_log", "saved_query",
    "faq_pair", "knowledge_doc", "sql_example", "relationship", "user_group",
    "workspace", "audit_log", "model_config", "menu", "user_group_role",
    "doc_chunk", "document_chunk", "kb_doc", "knowledge_chunk",
    "scheduled_task", "task_run_log", "sys_celery_task_log", "sys_crontab",
})
from ..security import aes_decrypt

logger = logging.getLogger(__name__)

_DIALECT_URL = {
    "mysql": "mysql+pymysql",
    "postgresql": "postgresql+psycopg2",
    "duckdb": "duckdb",
}
_DEFAULT_PORT = {"mysql": 3306, "postgresql": 5432, "clickhouse": 8123, "duckdb": 0}


def build_url(ds: Datasource) -> str:
    from urllib.parse import quote_plus
    scheme = _DIALECT_URL.get(ds.type, "mysql+pymysql")
    password = aes_decrypt(ds.password_enc)
    if ds.type == "duckdb":
        return "duckdb:///" + ds.db_name
    user = quote_plus(ds.user)
    pwd = quote_plus(password)
    return f"{scheme}://{user}:{pwd}@{ds.host}:{ds.port}/{ds.db_name}"


def test_connection(ds: Datasource) -> tuple[bool, str]:
    """测试连接；成功返回 (True, "ok")。"""
    try:
        engine = create_engine(build_url(ds), pool_pre_ping=True,
                               connect_args={"connect_timeout": 5})
        with engine.connect() as conn:
            if ds.type == "mysql":
                conn.exec_driver_sql("SELECT VERSION()")
            else:
                conn.exec_driver_sql("SELECT 1")
        engine.dispose()
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def sync_schema(ds: Datasource) -> dict:
    """采集 Schema：表/字段（含 comment）/主键/唯一索引/外键关系 → 写入元数据表。
    返回统计：{tables, columns, relationships}。"""
    engine = create_engine(build_url(ds), pool_pre_ping=True,
                           connect_args={"connect_timeout": 5} if ds.type == "mysql" else {})
    stats = {"tables": 0, "columns": 0, "relationships": 0, "dropped_relationships": 0}
    try:
        with engine.connect() as conn:
            schema = ds.db_name if ds.type == "mysql" else "public"
            # 1) 表
            rows = conn.execute(text(
                "SELECT TABLE_NAME, TABLE_COMMENT, TABLE_TYPE FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = :s AND TABLE_TYPE IN ('BASE TABLE','VIEW')"
            ), {"s": schema}).fetchall()
            table_map: dict[str, TableMeta] = {}
            db = SessionLocal()
            try:
                bound_ds = db.query(Datasource).get(ds.id)  # 绑定当前 session，状态修改可持久化
                if bound_ds:
                    ds = bound_ds
                # 豁免软删过滤：复用已被逻辑删除的同名表/字段（避免唯一约束冲突，并自动恢复）
                existing = {t.table_name: t for t in
                            db.query(TableMeta).execution_options(include_deleted=True)
                            .filter(TableMeta.datasource_id == ds.id).all()}
                for name, comment, ttype in rows:
                    name = str(name)
                    if name.lower() in _SYSTEM_TABLE_BLACKLIST:
                        continue  # 跳过系统表，避免混入业务数据源元数据
                    tm = existing.get(name)
                    if tm:
                        tm.comment = comment or ""
                        tm.table_type = ttype or "BASE TABLE"
                        tm.synced_at = datetime.utcnow()
                        tm.is_deleted = False
                    else:
                        tm = TableMeta(datasource_id=ds.id, schema_name=schema,
                                       table_name=name, comment=comment or "",
                                       table_type=ttype or "BASE TABLE", synced_at=datetime.utcnow())
                        db.add(tm)
                        db.flush()
                    table_map[name] = tm
                    stats["tables"] += 1

                # 2) 字段（含 comment）
                col_rows = conn.execute(text(
                    "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, COLUMN_COMMENT, "
                    "IS_NULLABLE, COLUMN_KEY, ORDINAL_POSITION, COLUMN_DEFAULT "
                    "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = :s "
                    "ORDER BY TABLE_NAME, ORDINAL_POSITION"
                ), {"s": schema}).fetchall()
                col_count: dict[int, int] = {}
                for tname, cname, dtype, comment, nullable, ckey, ordinal, default in col_rows:
                    tm = table_map.get(str(tname))
                    if not tm:
                        continue
                    col_count[tm.id] = col_count.get(tm.id, 0) + 1
                    existing_cols = {c.column_name: c for c in
                                     db.query(ColumnMeta).execution_options(include_deleted=True)
                                     .filter(ColumnMeta.table_meta_id == tm.id).all()}
                    col = existing_cols.get(str(cname))
                    if col:
                        col.data_type = str(dtype)
                        col.comment = comment or ""
                        col.is_nullable = str(nullable) == "YES"
                        col.is_pk = str(ckey) == "PRI"
                        col.default_value = str(default) if default is not None else ""
                        col.is_deleted = False
                    else:
                        col = ColumnMeta(
                            table_meta_id=tm.id, column_name=str(cname),
                            data_type=str(dtype), comment=comment or "",
                            is_nullable=str(nullable) == "YES",
                            is_pk=str(ckey) == "PRI",
                            ordinal=int(ordinal or 0),
                            default_value=str(default) if default is not None else "")
                        db.add(col)
                    stats["columns"] += 1

                # 字段统计回写（表字段数持久化）
                for tm in table_map.values():
                    tm.column_count = col_count.get(tm.id, 0)

                # 3) 外键关系（关系图信息）
                fk_rows = conn.execute(text(
                    "SELECT TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
                    "FROM information_schema.KEY_COLUMN_USAGE "
                    "WHERE TABLE_SCHEMA = :s AND REFERENCED_TABLE_NAME IS NOT NULL"
                ), {"s": schema}).fetchall()
                for tname, cname, ref_tname, ref_cname in fk_rows:
                    src_t = table_map.get(str(tname))
                    dst_t = table_map.get(str(ref_tname))
                    if not src_t or not dst_t:
                        continue
                    src_col = (db.query(ColumnMeta)
                               .filter(ColumnMeta.table_meta_id == src_t.id,
                                       ColumnMeta.column_name == str(cname)).first())
                    dst_col = (db.query(ColumnMeta)
                               .filter(ColumnMeta.table_meta_id == dst_t.id,
                                       ColumnMeta.column_name == str(ref_cname)).first())
                    if not src_col or not dst_col:
                        continue
                    dup = (db.query(Relationship).execution_options(include_deleted=True)
                           .filter(Relationship.datasource_id == ds.id,
                                   Relationship.src_table_id == src_t.id,
                                   Relationship.dst_table_id == dst_t.id,
                                   Relationship.source == "fk_auto").first())
                    if not dup:
                        db.add(Relationship(datasource_id=ds.id,
                                            src_table_id=src_t.id, src_col_id=src_col.id,
                                            dst_table_id=dst_t.id, dst_col_id=dst_col.id,
                                            rel_type="1:N", source="fk_auto", enabled=True))
                        stats["relationships"] += 1
                    else:
                        dup.src_col_id = src_col.id
                        dup.dst_col_id = dst_col.id
                        dup.is_deleted = False

                # 4) 清理已不存在的字段级外键关系
                kept_auto = {(r.src_table_id, r.src_col_id, r.dst_table_id, r.dst_col_id)
                             for r in db.query(Relationship)
                             .filter(Relationship.datasource_id == ds.id,
                                     Relationship.source == "fk_auto").all()}
                auto_rows = {
                    (str(tname), str(cname), str(ref_tname), str(ref_cname))
                    for tname, cname, ref_tname, ref_cname in fk_rows
                }
                for rel in db.query(Relationship).filter(
                        Relationship.datasource_id == ds.id,
                        Relationship.source == "fk_auto").all():
                    src_t = db.query(TableMeta).get(rel.src_table_id)
                    dst_t = db.query(TableMeta).get(rel.dst_table_id)
                    if not src_t or not dst_t:
                        continue
                    key = (src_t.table_name, "", dst_t.table_name, "")
                    if (src_t.table_name, "", dst_t.table_name, "") not in auto_rows:
                        rel.is_deleted = True
                        stats["dropped_relationships"] += 1

                ds.status = "ok"
                ds.last_sync_at = datetime.utcnow()
                db.commit()
            finally:
                db.close()
        return stats
    finally:
        engine.dispose()
