"""血缘：QueryLog SQL 挖掘 + 视图 DDL 挖掘 + 血缘查询。

- query_log 挖掘：解析生成 SQL 的 JOIN/WHERE 等值条件（a.x = b.y）→ 字段级边
  src=FROM 侧列 → dst=JOIN 侧列，confidence 低（30）
- 视图 DDL 挖掘：VIEW 定义中 FROM/JOIN 的表 → 表级边 src=基表 → dst=视图表，confidence 100
- 查询：按表取上游/下游（支持多级递归），返回前端 LineageGraph 结构
"""
import logging
import re
import traceback
from datetime import datetime

import sqlglot
from sqlglot import exp

from ..database import SessionLocal
from ..models import ColumnMeta, Datasource, LineageEdge, QueryLog, TableMeta

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[A-Za-z0-9_$]+$")

# source → 前端 source_type 映射
_SOURCE_TYPE_MAP = {
    "ddl": "view_ddl",
    "query_log": "query_log",
    "manual": "manual",
}


def _safe_name(name: str) -> bool:
    return bool(_NAME_RE.fullmatch(name or ""))


def _collect_table_aliases(select: exp.Select) -> dict[str, str]:
    """收集 SELECT 中所有表别名 → 真实表名（小写）。包含 FROM 和 JOIN。"""
    aliases: dict[str, str] = {}
    from_node = select.args.get("from_")
    if from_node and isinstance(from_node.this, exp.Table):
        t = from_node.this
        real = str(t.name).lower()
        aliases[real] = real
        if t.alias:
            aliases[str(t.alias).lower()] = real
    for j in (select.args.get("joins") or []):
        if isinstance(j.this, exp.Table):
            real = str(j.this.name).lower()
            aliases[real] = real
            if j.this.alias:
                aliases[str(j.this.alias).lower()] = real
    return aliases


def _resolve_table(alias_or_name: str | None, aliases: dict[str, str],
                   default: str | None) -> str | None:
    """将列的表前缀（可能是别名）解析为真实表名。"""
    if not alias_or_name:
        return default
    key = str(alias_or_name).lower()
    return aliases.get(key, key if _safe_name(key) else None)


def _extract_join_edges(sql: str, dialect: str) -> list[dict]:
    """从 SQL 提取 JOIN/WHERE 跨表等值条件边。
    处理表别名、CTE（跳过 CTE 内部 SELECT）、子查询递归。
    返回 [{"src_table","src_col","dst_table","dst_col"}]。"""
    edges: list[dict] = []
    try:
        ast = sqlglot.parse_one(sql, read=dialect)
    except Exception:
        return edges

    # 收集 CTE 名称（WITH 子句中的伪表名），避免将 CTE 当作真实表
    cte_names: set[str] = set()
    for cte in ast.find_all(exp.CTE):
        if cte.alias:
            cte_names.add(str(cte.alias).lower())

    def _process_select(select: exp.Select) -> None:
        aliases = _collect_table_aliases(select)
        from_table = None
        from_node = select.args.get("from_")
        if from_node and isinstance(from_node.this, exp.Table):
            from_table = str(from_node.this.name).lower()
            if from_table in cte_names:
                from_table = None

        # JOIN ON 等值
        for j in (select.args.get("joins") or []):
            jt = j.this if isinstance(j.this, exp.Table) else None
            on = j.args.get("on")
            if not jt or on is None:
                continue
            jname = str(jt.name).lower()
            if jname in cte_names:
                continue
            for eq in on.find_all(exp.EQ):
                l, r = eq.this, eq.expression
                lc = l if isinstance(l, exp.Column) else None
                rc = r if isinstance(r, exp.Column) else None
                if not (lc and rc):
                    continue
                lt = _resolve_table(str(lc.table) if lc.table else None, aliases, from_table)
                rt = _resolve_table(str(rc.table) if rc.table else None, aliases, jname)
                if lt and rt and lt != rt:
                    edges.append({"src_table": lt, "src_col": str(lc.name),
                                  "dst_table": rt, "dst_col": str(rc.name)})

        # WHERE 跨表等值（无 JOIN 的隐式关联）
        w = select.args.get("where")
        if w is not None and from_table:
            for eq in w.find_all(exp.EQ):
                l, r = eq.this, eq.expression
                lc = l if isinstance(l, exp.Column) else None
                rc = r if isinstance(r, exp.Column) else None
                if not (lc and rc):
                    continue
                lt = _resolve_table(str(lc.table) if lc.table else None, aliases, from_table)
                rt = _resolve_table(str(rc.table) if rc.table else None, aliases, from_table)
                if lt and rt and lt != rt:
                    edges.append({"src_table": lt, "src_col": str(lc.name),
                                  "dst_table": rt, "dst_col": str(rc.name)})

    # 只处理最外层 SELECT（跳过 CTE 内部的 SELECT，避免重复和伪表）
    if isinstance(ast, exp.Select):
        _process_select(ast)
    else:
        # 非 SELECT 语句（如 UNION），找所有顶层 SELECT
        for select in ast.find_all(exp.Select):
            # 跳过嵌套在 CTE 中的 SELECT
            parent = select
            while parent is not None:
                if isinstance(parent, exp.CTE):
                    break
                parent = getattr(parent, "parent", None)
            if isinstance(parent, exp.CTE):
                continue
            _process_select(select)

    return edges


def _table_ids_by_name(db, datasource_id: int) -> dict[str, int]:
    return {t.table_name.lower(): t.id for t in
            db.query(TableMeta).filter(TableMeta.datasource_id == datasource_id).all()}


def _col_ids_by_table(db, datasource_id: int) -> dict[tuple[int, str], int]:
    """(table_id, 列名小写) → column_id。"""
    out: dict[tuple[int, str], int] = {}
    for cm in (db.query(ColumnMeta)
               .join(TableMeta, TableMeta.id == ColumnMeta.table_meta_id)
               .filter(TableMeta.datasource_id == datasource_id).all()):
        out[(cm.table_meta_id, cm.column_name.lower())] = cm.id
    return out


def _upsert_edge(db, ws_id: int, ds_id: int, edge: dict) -> None:
    """幂等写入血缘边（同键更新 last_seen_at/confidence 取大）。"""
    existing = (db.query(LineageEdge)
                .filter(LineageEdge.workspace_id == ws_id,
                        LineageEdge.datasource_id == ds_id,
                        LineageEdge.src_table_id == edge["src_table_id"],
                        LineageEdge.src_column_id == edge.get("src_column_id"),
                        LineageEdge.dst_table_id == edge["dst_table_id"],
                        LineageEdge.dst_column_id == edge.get("dst_column_id"),
                        LineageEdge.source == edge["source"]).first())
    if existing:
        existing.last_seen_at = datetime.utcnow()
        existing.is_deleted = False
        existing.confidence = max(existing.confidence or 0, edge.get("confidence", 50))
        return
    db.add(LineageEdge(workspace_id=ws_id, datasource_id=ds_id,
                       src_table_id=edge["src_table_id"],
                       src_column_id=edge.get("src_column_id"),
                       dst_table_id=edge["dst_table_id"],
                       dst_column_id=edge.get("dst_column_id"),
                       edge_type=edge.get("edge_type", "query"),
                       source=edge["source"],
                       confidence=edge.get("confidence", 50),
                       last_seen_at=datetime.utcnow()))


def mine_query_logs(datasource_id: int, db=None) -> int:
    """从 QueryLog 挖掘 JOIN/WHERE 关联边。返回新增/更新边数。"""
    own_db = db is None
    if own_db:
        db = SessionLocal()
    n = 0
    parsed = 0
    skipped_table = 0
    skipped_col = 0
    try:
        ds = db.query(Datasource).get(datasource_id)
        if not ds:
            return 0
        dialect = ds.type
        tid_map = _table_ids_by_name(db, datasource_id)
        cid_map = _col_ids_by_table(db, datasource_id)
        # 挖掘所有有 generated_sql 的记录（不局限 executed=True），按时间倒序取最近 1000 条
        logs = (db.query(QueryLog)
                .filter(QueryLog.datasource_id == datasource_id,
                        QueryLog.generated_sql.isnot(None),
                        QueryLog.generated_sql != "")
                .order_by(QueryLog.id.desc())
                .limit(1000).all())
        logger.info("[lineage] ds=%s 待挖掘 QueryLog %d 条", datasource_id, len(logs))
        for log in logs:
            sql_edges = _extract_join_edges(log.generated_sql or "", dialect)
            if not sql_edges:
                continue
            parsed += 1
            for e in sql_edges:
                st = tid_map.get(e["src_table"].lower())
                dt = tid_map.get(e["dst_table"].lower())
                if not st or not dt:
                    skipped_table += 1
                    continue
                sc = cid_map.get((st, e["src_col"].lower()))
                dc = cid_map.get((dt, e["dst_col"].lower()))
                if not sc or not dc:
                    skipped_col += 1
                    continue
                _upsert_edge(db, log.workspace_id, datasource_id, {
                    "src_table_id": st, "src_column_id": sc,
                    "dst_table_id": dt, "dst_column_id": dc,
                    "edge_type": "query", "source": "query_log",
                    "confidence": 30})
                n += 1
        db.commit()
        logger.info("[lineage] ds=%s QueryLog 挖掘完成: 解析SQL %d 条, 写入边 %d, "
                    "表映射跳过 %d, 列映射跳过 %d",
                    datasource_id, parsed, n, skipped_table, skipped_col)
        return n
    finally:
        if own_db:
            db.close()


def mine_view_ddl(datasource_id: int, db=None, engine=None) -> int:
    """从数据源视图定义挖掘表级血缘边（source=ddl，置信度 100）。"""
    from ..services.datasource_service import build_url

    own_db = db is None
    if own_db:
        db = SessionLocal()
    n = 0
    try:
        ds = db.query(Datasource).get(datasource_id)
        if not ds:
            return 0
        from sqlalchemy import create_engine, text

        engine = engine or create_engine(
            build_url(ds), pool_pre_ping=True,
            connect_args={"connect_timeout": 5} if ds.type == "mysql" else {})
        tid_map = _table_ids_by_name(db, datasource_id)
        views = []
        try:
            with engine.connect() as conn:
                if ds.type == "mysql":
                    views = conn.execute(text(
                        "SELECT TABLE_NAME, VIEW_DEFINITION FROM information_schema.VIEWS "
                        "WHERE TABLE_SCHEMA = :s"), {"s": ds.db_name}).fetchall()
                else:
                    views = conn.execute(text(
                        "SELECT viewname, definition FROM pg_views WHERE schemaname = :s"),
                        {"s": "public"}).fetchall()
        except Exception as exc:
            logger.warning("[lineage] ds=%s 视图定义读取失败: %s\n%s",
                           datasource_id, repr(exc), traceback.format_exc())
            views = []

        logger.info("[lineage] ds=%s 发现视图 %d 个", datasource_id, len(views))
        for vname, vdef in views:
            vname = str(vname)
            vid = tid_map.get(vname.lower())
            if not vid or not (vdef or "").strip():
                continue
            try:
                ast = sqlglot.parse_one(vdef, read=ds.type)
            except Exception as exc:
                logger.warning("[lineage] ds=%s 视图 %s DDL 解析失败: %s",
                               datasource_id, vname, repr(exc))
                continue
            # 收集视图定义中所有真实表引用（排除 CTE 伪表）
            cte_names: set[str] = set()
            for cte in ast.find_all(exp.CTE):
                if cte.alias:
                    cte_names.add(str(cte.alias).lower())
            base_tables: set[int] = set()
            for t in ast.find_all(exp.Table):
                tname = str(t.name).lower()
                if tname in cte_names:
                    continue
                base = tid_map.get(tname)
                if base and base != vid:
                    base_tables.add(base)
            for base in base_tables:
                _upsert_edge(db, ds.workspace_id, datasource_id, {
                    "src_table_id": base, "src_column_id": None,
                    "dst_table_id": vid, "dst_column_id": None,
                    "edge_type": "view", "source": "ddl", "confidence": 100})
                n += 1
        db.commit()
        logger.info("[lineage] ds=%s 视图 DDL 挖掘完成: 写入表级边 %d", datasource_id, n)
        return n
    except Exception as exc:
        logger.warning("[lineage] ds=%s 视图挖掘异常: %s\n%s",
                       datasource_id, repr(exc), traceback.format_exc())
        return n
    finally:
        if engine is not None:
            engine.dispose()
        if own_db:
            db.close()


def get_lineage(db, ws_id: int, table_id: int, direction: str = "both",
                max_depth: int = 2) -> dict:
    """查询指定表的血缘图（支持多级递归），返回前端 LineageGraph 结构。

    direction: upstream / downstream / both
    max_depth: 递归深度（默认 2 级）
    """
    table = db.query(TableMeta).get(table_id)
    if not table:
        return {"nodes": [], "edges": [], "root": ""}

    # 加载该数据源所有血缘边（内存中过滤，数据量可控）
    all_edges = (db.query(LineageEdge)
                 .filter(LineageEdge.workspace_id == ws_id,
                         LineageEdge.datasource_id == table.datasource_id,
                         LineageEdge.is_deleted == False).all())

    # BFS 递归收集关联表和边
    visited_tables: set[int] = {table_id}
    result_edges: list[LineageEdge] = []
    current_level: set[int] = {table_id}

    for _ in range(max_depth):
        next_level: set[int] = set()
        for e in all_edges:
            # 下游：当前表是 src
            if direction in ("downstream", "both") and e.src_table_id in current_level:
                if e not in result_edges:
                    result_edges.append(e)
                if e.dst_table_id not in visited_tables:
                    next_level.add(e.dst_table_id)
            # 上游：当前表是 dst
            if direction in ("upstream", "both") and e.dst_table_id in current_level:
                if e not in result_edges:
                    result_edges.append(e)
                if e.src_table_id not in visited_tables:
                    next_level.add(e.src_table_id)
        if not next_level:
            break
        visited_tables.update(next_level)
        current_level = next_level

    # 批量加载表和列信息
    table_map: dict[int, TableMeta] = {}
    for tid in visited_tables:
        tm = db.query(TableMeta).get(tid)
        if tm:
            table_map[tid] = tm

    col_map: dict[int, ColumnMeta] = {}
    col_ids = {e.src_column_id for e in result_edges if e.src_column_id} | \
              {e.dst_column_id for e in result_edges if e.dst_column_id}
    for cid in col_ids:
        cm = db.query(ColumnMeta).get(cid)
        if cm:
            col_map[cid] = cm

    # 构建节点（category: 0=当前表, 1=上游, 2=下游）
    nodes = []
    for tid in sorted(visited_tables):
        tm = table_map.get(tid)
        if not tm:
            continue
        if tid == table_id:
            category = 0
        elif any(e.src_table_id == tid for e in result_edges):
            # 该表作为 src 出现在边中 → 它是上游（被依赖）
            category = 1
        else:
            category = 2
        nodes.append({
            "id": str(tid),
            "name": tm.table_name,
            "table_id": tid,
            "category": category,
            "source": tm.table_type or "table",
            "value": tm.comment or "",
        })

    # 构建边（source/target 为字符串 table_id，对齐 ECharts graph）
    edges = []
    for e in result_edges:
        src_col = col_map.get(e.src_column_id) if e.src_column_id else None
        dst_col = col_map.get(e.dst_column_id) if e.dst_column_id else None
        source_type = _SOURCE_TYPE_MAP.get(e.source, e.source or "query_log")
        edges.append({
            "source": str(e.src_table_id),
            "target": str(e.dst_table_id),
            "label": e.edge_type or "",
            "src_col": src_col.column_name if src_col else "",
            "dst_col": dst_col.column_name if dst_col else "",
            "source_type": source_type,
            "confidence": e.confidence or 0,
        })

    return {
        "nodes": nodes,
        "edges": edges,
        "root": str(table_id),
        "table_name": table.table_name,
    }
