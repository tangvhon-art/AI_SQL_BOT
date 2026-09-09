"""数据源服务：连接测试、Schema 采集（表/字段 comment、主键/唯一索引、外键关系图信息）。"""
import logging
from datetime import datetime

from sqlalchemy import create_engine, text
from ..database import SessionLocal
from ..models import ColumnMeta, Datasource, Relationship, TableMeta

logger = logging.getLogger(__name__)

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


_SKIP_SAMPLE_DTYPES = frozenset({
    "text", "longtext", "mediumtext", "tinytext", "blob", "longblob",
    "mediumblob", "tinyblob", "json", "geometry", "point", "linestring",
    "polygon", "date", "datetime", "timestamp", "time",
})


def _collect_column_samples(conn, db, ds: Datasource, table_map: dict) -> int:
    """对低基数字段采集 top-N 样例值（C7）。
    仅 MySQL/PostgreSQL；表行数 < sample_min_rows 跳过；单列查询超时 3s 跳过；
    表名/列名仅允许 [A-Za-z0-9_$]（防注入）。返回采集字段数。"""
    from ..config import get_settings
    from ..models import ColumnSample

    settings = get_settings()
    if not settings.schema_sync_collect_samples or ds.type not in ("mysql", "postgresql"):
        return 0
    import re as _re

    q = "`" if ds.type == "mysql" else '"'
    timeout_ms = int(settings.schema_sync_sample_timeout_s * 1000)
    limit = settings.schema_sync_sample_per_column
    if ds.type == "mysql":
        conn.exec_driver_sql(f"SET SESSION MAX_EXECUTION_TIME = {timeout_ms}")
    collected = 0
    try:
        for tm in table_map.values():
            if str(tm.table_type).upper() != "BASE TABLE":
                continue
            tname = tm.table_name
            if not _re.fullmatch(r"[A-Za-z0-9_$]+", tname):
                continue
            # 行数估算：不足 min_rows 的表不采集
            try:
                if ds.type == "mysql":
                    est = conn.execute(text(
                        "SELECT TABLE_ROWS FROM information_schema.TABLES "
                        "WHERE TABLE_SCHEMA = :s AND TABLE_NAME = :t"
                    ), {"s": ds.db_name, "t": tname}).scalar()
                else:
                    est = conn.execute(text(
                        "SELECT c.reltuples::bigint FROM pg_class c "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = :s AND c.relname = :t"
                    ), {"s": "public", "t": tname}).scalar()
                if est is not None and est < settings.schema_sync_sample_min_rows:
                    continue
            except Exception:  # noqa: BLE001
                pass
            cols = (db.query(ColumnMeta)
                    .filter(ColumnMeta.table_meta_id == tm.id)
                    .order_by(ColumnMeta.ordinal.asc()).all())
            for col in cols:
                cname = col.column_name
                if col.is_pk or not cname or not _re.fullmatch(r"[A-Za-z0-9_$]+", cname):
                    continue
                if (col.data_type or "").lower() in _SKIP_SAMPLE_DTYPES:
                    continue
                try:
                    sql = (f"SELECT {q}{cname}{q} AS v, COUNT(*) AS c FROM {q}{tname}{q} "
                           f"GROUP BY {q}{cname}{q} ORDER BY c DESC LIMIT {limit}")
                    rows = conn.execute(text(sql)).fetchall()
                except Exception:  # noqa: BLE001  （超时/权限不足/类型不支持 → 跳过该列）
                    continue
                if not rows:
                    continue
                samples = [{"value": str(v)[:128], "freq": int(f or 0)} for v, f in rows]
                col.samples_json = samples
                db.query(ColumnSample).filter(
                    ColumnSample.column_meta_id == col.id).delete()
                for s in samples:
                    db.add(ColumnSample(column_meta_id=col.id,
                                        sample_value=s["value"], freq=s["freq"],
                                        sample_type="top"))
                collected += 1
    finally:
        if ds.type == "mysql":
            try:
                conn.exec_driver_sql("SET SESSION MAX_EXECUTION_TIME = 0")
            except Exception:  # noqa: BLE001
                pass
    return collected


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
    stats = {"tables": 0, "columns": 0, "relationships": 0, "inferred_relationships": 0, "dropped_relationships": 0}
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

                # C7 样例值采集（低基数字段 top-N，statement timeout 保护，MySQL/PostgreSQL）
                try:
                    stats["samples"] = _collect_column_samples(conn, db, ds, table_map)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("样例值采集失败（不影响 Schema 采集）: %s", exc)
                    stats["samples"] = 0

                # 3) 外键关系（关系图信息）—— MySQL/PostgreSQL 双适配 + 容错
                fk_rows = []
                try:
                    if ds.type == "postgresql":
                        fk_rows = conn.execute(text(
                            "SELECT tc.table_name, kcu.column_name, ccu.table_name AS ref_table, "
                            "ccu.column_name AS ref_col "
                            "FROM information_schema.table_constraints tc "
                            "JOIN information_schema.key_column_usage kcu "
                            "  ON tc.constraint_name = kcu.constraint_name "
                            "  AND tc.table_schema = kcu.table_schema "
                            "JOIN information_schema.constraint_column_usage ccu "
                            "  ON tc.constraint_name = ccu.constraint_name "
                            "  AND tc.table_schema = ccu.table_schema "
                            "WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = :s"
                        ), {"s": schema}).fetchall()
                    else:
                        fk_rows = conn.execute(text(
                            "SELECT TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
                            "FROM information_schema.KEY_COLUMN_USAGE "
                            "WHERE TABLE_SCHEMA = :s AND REFERENCED_TABLE_NAME IS NOT NULL"
                        ), {"s": schema}).fetchall()
                except Exception as fk_err:  # noqa: BLE001
                    logger.warning("外键采集失败（不影响表/字段采集）: %s", fk_err)

                # 表名大小写不敏感映射（部分数据库表名大小写敏感）
                table_map_ci = {k.lower(): v for k, v in table_map.items()}
                for row in fk_rows:
                    tname, cname, ref_tname, ref_cname = row[0], row[1], row[2], row[3]
                    src_t = table_map.get(str(tname)) or table_map_ci.get(str(tname).lower())
                    dst_t = table_map.get(str(ref_tname)) or table_map_ci.get(str(ref_tname).lower())
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
                                   Relationship.src_col_id == src_col.id,
                                   Relationship.dst_table_id == dst_t.id,
                                   Relationship.dst_col_id == dst_col.id,
                                   Relationship.source == "fk_auto").first())
                    if not dup:
                        db.add(Relationship(datasource_id=ds.id,
                                            src_table_id=src_t.id, src_col_id=src_col.id,
                                            dst_table_id=dst_t.id, dst_col_id=dst_col.id,
                                            rel_type="1:N", source="fk_auto", enabled=True))
                        stats["relationships"] += 1
                    else:
                        dup.is_deleted = False

                # 3.5) 逻辑外键推断——无物理外键的数据库，根据字段名约定（xxx_id → xxxs.id）推断
                # 预加载所有表的字段到内存，避免重复查询
                all_cols_by_table: dict[int, list[ColumnMeta]] = {}
                for tm in table_map.values():
                    all_cols_by_table[tm.id] = (db.query(ColumnMeta)
                                                  .filter(ColumnMeta.table_meta_id == tm.id).all())
                # 表名索引（小写 → TableMeta）
                table_name_index: dict[str, TableMeta] = {}
                for tname, tm in table_map.items():
                    table_name_index[tname.lower()] = tm
                # 已存在的物理外键集合（避免重复推断）
                existing_fk = {(r.src_table_id, r.src_col_id, r.dst_table_id, r.dst_col_id)
                               for r in db.query(Relationship)
                               .filter(Relationship.datasource_id == ds.id,
                                       Relationship.source == "fk_auto").all()}
                # 先软删除所有旧的推断关系，推断时恢复匹配的（避免残留错误推断）
                old_inferred = (db.query(Relationship)
                                .filter(Relationship.datasource_id == ds.id,
                                        Relationship.source == "inferred",
                                        Relationship.is_deleted == False).all())
                for r in old_inferred:
                    r.is_deleted = True
                # 用 (src_table_id, src_col_id, dst_table_id) 做旧关系索引，推断时恢复
                old_inferred_index = {(r.src_table_id, r.src_col_id, r.dst_table_id): r
                                       for r in old_inferred}
                # 常见非外键字段前缀（这些字段通常不是表关联外键）
                _NON_FK_PREFIXES = frozenset({
                    "guid", "uid", "uuid", "open", "union", "app", "calendar",
                    "device", "token", "session", "code", "key", "hash",
                })

                def _find_target_table(pfx: str, src_table_id: int) -> TableMeta | None:
                    """根据前缀查找目标表：精确匹配优先，业务表后缀次之，词根还原，前缀模糊匹配最后。"""
                    # 0. 排除备份/日志/历史表（不适合作为关联目标）
                    def _is_backup(tname: str) -> bool:
                        return any(k in tname for k in ("_bak", "_log", "_history", "_2024", "_2023",
                                                          "_2022", "_2021", "_copy", "_tmp", "_test_bak"))

                    # 词根还原（meeting→meet, organization→organ, running→run）
                    def _stem(w: str) -> str:
                        for suffix in ("ing", "tion", "ment", "ness", "ity", "ance", "ence"):
                            if w.endswith(suffix) and len(w) > len(suffix) + 2:
                                stem = w[:-len(suffix)]
                                # 双写辅音还原（running→run）
                                if len(stem) >= 3 and stem[-1] == stem[-2]:
                                    stem = stem[:-1]
                                return stem
                        return w

                    # 待尝试的前缀列表：原前缀 + 词根还原
                    prefixes_to_try = [pfx]
                    stemmed = _stem(pfx)
                    if stemmed != pfx and len(stemmed) >= 3:
                        prefixes_to_try.append(stemmed)

                    for try_pfx in prefixes_to_try:
                        # 1. 精确匹配（单数/复数）
                        candidates = [try_pfx, try_pfx + "s", try_pfx + "es"]
                        if try_pfx.endswith("y"):
                            candidates.append(try_pfx[:-1] + "ies")
                        for cand in candidates:
                            if cand in table_name_index and not _is_backup(cand):
                                return table_name_index[cand]
                        # 2. 业务表后缀优先匹配
                        _BIZ_SUFFIXES = ("_info", "_record", "_detail", "_list", "_config",
                                          "_setting", "_type", "_category", "_dict", "_item",
                                          "_definition", "_def", "_meta", "_data")
                        for suffix in _BIZ_SUFFIXES:
                            cand = try_pfx + suffix
                            if cand in table_name_index and not _is_backup(cand):
                                return table_name_index[cand]

                    # 3. 前缀模糊匹配（表名以 prefix_ 开头，排除非主表后缀）
                    _NON_TARGET_SUFFIXES = ("_pad", "_log", "_history", "_bak", "_record_log",
                                             "_effect", "_restart", "_fail_record", "_statistics")
                    if len(pfx) >= 3:
                        best = None
                        best_len = 999
                        for tname_lower, tm in table_name_index.items():
                            if (tname_lower.startswith(pfx + "_") and tname_lower != pfx
                                    and len(tname_lower) <= len(pfx) * 2.5
                                    and tm.id != src_table_id
                                    and not _is_backup(tname_lower)
                                    and not any(tname_lower.endswith(s) for s in _NON_TARGET_SUFFIXES)):
                                if len(tname_lower) < best_len:
                                    best = tm
                                    best_len = len(tname_lower)
                        if best:
                            return best
                    return None

                inferred_count = 0
                for src_tm in table_map.values():
                    for col in all_cols_by_table.get(src_tm.id, []):
                        cname = col.column_name.lower()
                        # 匹配 xxx_id 格式（排除纯 id 字段）
                        if not cname.endswith("_id") or len(cname) <= 3:
                            continue
                        prefix = cname[:-3]
                        # 排除常见非外键字段
                        if prefix in _NON_FK_PREFIXES:
                            continue
                        # 查找目标表
                        dst_tm = _find_target_table(prefix, src_tm.id)
                        if not dst_tm:
                            continue
                        # 自引用也保留（如 parent_id → 本表.id），但排除纯 id
                        # 目标表必须有 id 字段
                        dst_cols = all_cols_by_table.get(dst_tm.id, [])
                        dst_id_col = next((c for c in dst_cols if c.column_name.lower() == "id"), None)
                        if not dst_id_col:
                            continue
                        # 避免与物理外键重复
                        fk_key = (src_tm.id, col.id, dst_tm.id, dst_id_col.id)
                        if fk_key in existing_fk:
                            continue
                        # 恢复旧推断关系或新建
                        old_rel = old_inferred_index.get((src_tm.id, col.id, dst_tm.id))
                        if old_rel:
                            old_rel.is_deleted = False
                            old_rel.dst_col_id = dst_id_col.id
                        else:
                            db.add(Relationship(datasource_id=ds.id,
                                                src_table_id=src_tm.id, src_col_id=col.id,
                                                dst_table_id=dst_tm.id, dst_col_id=dst_id_col.id,
                                                rel_type="1:N", source="inferred", enabled=True))
                        inferred_count += 1
                stats["inferred_relationships"] = inferred_count
                logger.info("逻辑外键推断: %d 条（数据源 %s）", inferred_count, ds.name)

                # 4) 清理已不存在的外键关系（按 表+字段 精确匹配）
                auto_rows = {
                    (str(tname).lower(), str(cname).lower(), str(ref_tname).lower(), str(ref_cname).lower())
                    for tname, cname, ref_tname, ref_cname in fk_rows
                }
                for rel in db.query(Relationship).filter(
                        Relationship.datasource_id == ds.id,
                        Relationship.source == "fk_auto").all():
                    src_t = db.query(TableMeta).get(rel.src_table_id)
                    dst_t = db.query(TableMeta).get(rel.dst_table_id)
                    src_col = db.query(ColumnMeta).get(rel.src_col_id)
                    dst_col = db.query(ColumnMeta).get(rel.dst_col_id)
                    if not src_t or not dst_t or not src_col or not dst_col:
                        rel.is_deleted = True
                        stats["dropped_relationships"] += 1
                        continue
                    key = (src_t.table_name.lower(), src_col.column_name.lower(),
                           dst_t.table_name.lower(), dst_col.column_name.lower())
                    if key not in auto_rows:
                        rel.is_deleted = True
                        stats["dropped_relationships"] += 1

                ds.status = "ok"
                ds.last_sync_at = datetime.utcnow()
                db.commit()
                # C1 缓存失效：Schema 已变化，清空该数据源生成/结果缓存
                from ..engine.cache_manager import invalidate_for_datasource
                try:
                    invalidate_for_datasource(ds.id, db)
                    db.commit()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[cache] schema 同步后缓存失效失败: %s", exc)
            finally:
                db.close()
        return stats
    finally:
        engine.dispose()
