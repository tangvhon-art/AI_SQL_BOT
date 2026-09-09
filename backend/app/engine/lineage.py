"""C8 · 血缘：QueryLog SQL 挖掘 + 视图 DDL 挖掘 + 血缘查询。

- query_log 挖掘：解析生成 SQL 的 JOIN/WHERE 等值条件（a.x = b.y）→ 边
  src=FROM 侧列 → dst=JOIN 侧列（维度补充方向），confidence 低（30）
- 视图 DDL 挖掘：VIEW 定义中 FROM/JOIN 的表 → 表级边 src=基表 → dst=视图表，confidence 100
- 查询：按表取上游/下游（字段级优先展示），供 API 与前端血缘图使用
"""
import logging
import re
from datetime import datetime

import sqlglot
from sqlglot import exp

from ..database import SessionLocal
from ..models import ColumnMeta, Datasource, LineageEdge, QueryLog, TableMeta

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[A-Za-z0-9_$]+$")


def _safe_name(name: str) -> bool:
    return bool(_NAME_RE.fullmatch(name or ""))


def _extract_join_edges(sql: str, dialect: str) -> list[dict]:
    """从 SQL 提取 JOIN/WHERE 跨表等值条件边：
    [{"src_table","src_col","dst_table","dst_col"}]。"""
    edges: list[dict] = []
    try:
        ast = sqlglot.parse_one(sql, read=dialect)
    except Exception:  # noqa: BLE001
        return edges
    for select in ast.find_all(exp.Select):
        from_table = None
        from_node = select.args.get("from_")
        if from_node and isinstance(from_node.this, exp.Table):
            from_table = str(from_node.this.name)
        # JOIN ON 等值
        for j in (select.args.get("joins") or []):
            jt = j.this if isinstance(j.this, exp.Table) else None
            on = j.args.get("on")
            if not jt or on is None:
                continue
            jname = str(jt.name)
            for eq in on.find_all(exp.EQ):
                l, r = eq.this, eq.expression
                lc = l if isinstance(l, exp.Column) else None
                rc = r if isinstance(r, exp.Column) else None
                if lc and rc:
                    edges.append({
                        "src_table": from_table or jname, "src_col": str(lc.name),
                        "dst_table": jname, "dst_col": str(rc.name)})
        # WHERE 跨表等值（无 JOIN 的隐式关联）
        w = select.args.get("where")
        if w is not None and from_table:
            for eq in w.find_all(exp.EQ):
                l, r = eq.this, eq.expression
                lc = l if isinstance(l, exp.Column) else None
                rc = r if isinstance(r, exp.Column) else None
                if lc and rc:
                    lt = str(lc.table) if lc.table else from_table
                    rt = str(rc.table) if rc.table else from_table
                    if lt and rt and lt.lower() != rt.lower():
                        edges.append({"src_table": lt, "src_col": str(lc.name),
                                      "dst_table": rt, "dst_col": str(rc.name)})
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
    try:
        ds = db.query(Datasource).get(datasource_id)
        if not ds:
            return 0
        dialect = ds.type
        tid_map = _table_ids_by_name(db, datasource_id)
        cid_map = _col_ids_by_table(db, datasource_id)
        logs = (db.query(QueryLog)
                .filter(QueryLog.datasource_id == datasource_id,
                        QueryLog.executed.is_(True))
                .order_by(QueryLog.id.desc())
                .limit(500).all())
        for log in logs:
            for e in _extract_join_edges(log.generated_sql or "", dialect):
                st, dt = tid_map.get(e["src_table"].lower()), tid_map.get(e["dst_table"].lower())
                if not st or not dt:
                    continue
                sc = cid_map.get((st, e["src_col"].lower()))
                dc = cid_map.get((dt, e["dst_col"].lower()))
                if not sc or not dc:
                    continue
                _upsert_edge(db, log.workspace_id, datasource_id, {
                    "src_table_id": st, "src_column_id": sc,
                    "dst_table_id": dt, "dst_column_id": dc,
                    "edge_type": "query", "source": "query_log",
                    "confidence": 30})
                n += 1
        db.commit()
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
        except Exception as exc:  # noqa: BLE001
            logger.warning("[lineage] 视图定义读取失败: %s", exc)
            views = []
        for vname, vdef in views:
            vname = str(vname)
            vid = tid_map.get(vname.lower())
            if not vid or not (vdef or "").strip():
                continue
            try:
                ast = sqlglot.parse_one(vdef, read=ds.type)
            except Exception:  # noqa: BLE001
                continue
            for t in ast.find_all(exp.Table):
                base = tid_map.get(str(t.name).lower())
                if base and base != vid:
                    _upsert_edge(db, ds.workspace_id, datasource_id, {
                        "src_table_id": base, "src_column_id": None,
                        "dst_table_id": vid, "dst_column_id": None,
                        "edge_type": "view", "source": "ddl", "confidence": 100})
                    n += 1
        db.commit()
        return n
    finally:
        if engine is not None:
            engine.dispose()
        if own_db:
            db.close()


def get_lineage(db, ws_id: int, table_id: int, direction: str = "both") -> dict:
    """查询指定表的血缘图：{table_id, nodes:[{table_id,name,comment,type}], edges:[...]}。
    direction: upstream / downstream / both。"""
    table = db.query(TableMeta).get(table_id)
    if not table:
        return {"table_id": table_id, "nodes": [], "edges": []}
    edges = (db.query(LineageEdge)
             .filter(LineageEdge.workspace_id == ws_id,
                     LineageEdge.datasource_id == table.datasource_id).all())
    node_ids: set[int] = {table_id}
    edge_out = []
    for e in edges:
        hit = ((direction in ("downstream", "both") and e.src_table_id == table_id) or
               (direction in ("upstream", "both") and e.dst_table_id == table_id))
        if not hit:
            continue
        node_ids.update({e.src_table_id, e.dst_table_id})
        src_col = db.query(ColumnMeta).get(e.src_column_id) if e.src_column_id else None
        dst_col = db.query(ColumnMeta).get(e.dst_column_id) if e.dst_column_id else None
        edge_out.append({
            "id": e.id, "src_table_id": e.src_table_id,
            "src_column": src_col.column_name if src_col else "",
            "dst_table_id": e.dst_table_id,
            "dst_column": dst_col.column_name if dst_col else "",
            "edge_type": e.edge_type, "source": e.source,
            "confidence": e.confidence,
        })
    nodes = []
    for nid in sorted(node_ids):
        tm = db.query(TableMeta).get(nid)
        if tm:
            nodes.append({"table_id": tm.id, "name": tm.table_name,
                          "comment": tm.comment or "", "type": tm.table_type or ""})
    return {"table_id": table_id, "nodes": nodes, "edges": edge_out,
            "table_name": table.table_name}
