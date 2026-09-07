"""数据源管理：CRUD / 连接测试 / Schema 采集 / 元数据与关系 / ER 图。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db, soft_delete_all
from ..models import ColumnMeta, Datasource, Relationship, TableMeta
from ..security import aes_decrypt, aes_encrypt
from ..services.datasource_service import sync_schema, test_connection
from .deps import get_current_user

router = APIRouter(prefix="/datasources", tags=["datasources"])


class DatasourceIn(BaseModel):
    name: str
    type: str = "mysql"
    host: str = "localhost"
    port: int = 3306
    db_name: str = ""
    user: str = ""
    password: str = ""
    params_json: dict = {}


class RelationIn(BaseModel):
    src_table_id: int
    src_col_id: int
    dst_table_id: int
    dst_col_id: int
    rel_type: str = "1:N"


def _out(ds: Datasource) -> dict:
    return {"id": ds.id, "workspace_id": ds.workspace_id, "name": ds.name,
            "type": ds.type, "host": ds.host, "port": ds.port, "db_name": ds.db_name,
            "user": ds.user, "status": ds.status, "last_sync_at": ds.last_sync_at,
            "params_json": ds.params_json or {}}


@router.get("")
def list_datasources(db: Session = Depends(get_db), user=Depends(get_current_user)):
    return [_out(ds) for ds in db.query(Datasource)
            .filter(Datasource.workspace_id == user.workspace_id).all()]


@router.post("")
def create_datasource(body: DatasourceIn, db: Session = Depends(get_db),
                      user=Depends(get_current_user)):
    ds = Datasource(workspace_id=user.workspace_id, name=body.name, type=body.type,
                    host=body.host, port=body.port or {"mysql": 3306}.get(body.type, 3306),
                    db_name=body.db_name, user=body.user,
                    password_enc=aes_encrypt(body.password), params_json=body.params_json)
    db.add(ds)
    db.commit()
    db.refresh(ds)
    return _out(ds)


@router.put("/{ds_id}")
def update_datasource(ds_id: int, body: DatasourceIn, db: Session = Depends(get_db),
                      user=Depends(get_current_user)):
    ds = db.query(Datasource).get(ds_id)
    if not ds or ds.workspace_id != user.workspace_id:
        raise HTTPException(404, "数据源不存在")
    for f in ("name", "type", "host", "port", "db_name", "user", "params_json"):
        setattr(ds, f, getattr(body, f))
    if body.password:
        ds.password_enc = aes_encrypt(body.password)
    db.commit()
    return _out(ds)


@router.delete("/{ds_id}")
def delete_datasource(ds_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    ds = db.query(Datasource).get(ds_id)
    if not ds or ds.workspace_id != user.workspace_id:
        raise HTTPException(404, "数据源不存在")
    soft_delete_all(db.query(Relationship).filter(Relationship.datasource_id == ds_id))
    tbl_ids = [t.id for t in db.query(TableMeta)
               .filter(TableMeta.datasource_id == ds_id).all()]
    if tbl_ids:
        soft_delete_all(db.query(ColumnMeta).filter(ColumnMeta.table_meta_id.in_(tbl_ids)))
        soft_delete_all(db.query(TableMeta).filter(TableMeta.id.in_(tbl_ids)))
    ds.is_deleted = True
    db.commit()
    return {"ok": True}


@router.post("/{ds_id}/test")
def test(ds_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    ds = db.query(Datasource).get(ds_id)
    if not ds or ds.workspace_id != user.workspace_id:
        raise HTTPException(404, "数据源不存在")
    ok, msg = test_connection(ds)
    ds.status = "ok" if ok else "failed"
    db.commit()
    return {"ok": ok, "message": msg}


@router.post("/{ds_id}/sync-schema")
def sync(ds_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    ds = db.query(Datasource).get(ds_id)
    if not ds or ds.workspace_id != user.workspace_id:
        raise HTTPException(404, "数据源不存在")
    ds.status = "syncing"
    db.commit()
    try:
        stats = sync_schema(ds)
    except Exception as exc:  # noqa: BLE001
        ds.status = "failed"
        db.commit()
        raise HTTPException(400, f"Schema 采集失败: {exc}") from exc
    return {"ok": True, "stats": stats}


@router.get("/{ds_id}/schemas")
def schemas(ds_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """返回数据源已采集的 schema（项目/库）清单，供问数前端选择查询范围。"""
    from ..engine.mapping import fetch_schemas
    return fetch_schemas(ds_id)


@router.get("/{ds_id}/tables")
def tables(ds_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    rows = (db.query(TableMeta, func.count(ColumnMeta.id))
            .outerjoin(ColumnMeta, ColumnMeta.table_meta_id == TableMeta.id)
            .filter(TableMeta.datasource_id == ds_id)
            .group_by(TableMeta.id)
            .order_by(TableMeta.table_name).all())
    return [{"id": t.id, "schema": t.schema_name, "table_name": t.table_name,
             "comment": t.comment, "table_type": t.table_type,
             "column_count": cnt, "deprecated": t.deprecated} for t, cnt in rows]


@router.get("/tables/{table_id}/columns")
def columns(table_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    cols = (db.query(ColumnMeta).filter(ColumnMeta.table_meta_id == table_id)
            .order_by(ColumnMeta.ordinal).all())
    return [{"id": c.id, "column_name": c.column_name, "data_type": c.data_type,
             "comment": c.comment, "is_nullable": c.is_nullable, "is_pk": c.is_pk,
             "default_value": c.default_value} for c in cols]


class CommentIn(BaseModel):
    comment: str = ""


@router.put("/columns/{col_id}/comment")
def update_comment(col_id: int, body: CommentIn, db: Session = Depends(get_db),
                   user=Depends(get_current_user)):
    col = db.query(ColumnMeta).get(col_id)
    if not col:
        raise HTTPException(404, "字段不存在")
    col.comment = body.comment
    db.commit()
    return {"ok": True}


@router.get("/{ds_id}/relationships")
def list_relationships(ds_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    rels = db.query(Relationship).filter(Relationship.datasource_id == ds_id).all()
    out = []
    for r in rels:
        src_t = db.query(TableMeta).get(r.src_table_id)
        dst_t = db.query(TableMeta).get(r.dst_table_id)
        src_c = db.query(ColumnMeta).get(r.src_col_id)
        dst_c = db.query(ColumnMeta).get(r.dst_col_id)
        if not all([src_t, dst_t, src_c, dst_c]):
            continue
        out.append({"id": r.id, "src_table": src_t.table_name, "src_col": src_c.column_name,
                    "dst_table": dst_t.table_name, "dst_col": dst_c.column_name,
                    "rel_type": r.rel_type, "source": r.source, "enabled": r.enabled})
    return out


@router.post("/{ds_id}/relationships")
def create_relationship(ds_id: int, body: RelationIn, db: Session = Depends(get_db),
                        user=Depends(get_current_user)):
    rel = Relationship(datasource_id=ds_id, src_table_id=body.src_table_id,
                       src_col_id=body.src_col_id, dst_table_id=body.dst_table_id,
                       dst_col_id=body.dst_col_id, rel_type=body.rel_type,
                       source="manual", enabled=True)
    db.add(rel)
    db.commit()
    return {"ok": True}


@router.delete("/relationships/{rel_id}")
def delete_relationship(rel_id: int, db: Session = Depends(get_db),
                        user=Depends(get_current_user)):
    rel = db.query(Relationship).get(rel_id)
    if rel:
        rel.is_deleted = True
        db.commit()
    return {"ok": True}


@router.get("/{ds_id}/er-graph")
def er_graph(ds_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """ER 关系图数据（前端 ECharts graph 渲染）。"""
    tables = db.query(TableMeta).filter(TableMeta.datasource_id == ds_id).all()
    rels = db.query(Relationship).filter(Relationship.datasource_id == ds_id,
                                         Relationship.enabled.is_(True)).all()
    nodes, edges, seen_e = [], [], set()
    for i, t in enumerate(tables):
        nodes.append({"id": f"t{t.id}", "name": t.table_name,
                      "comment": t.comment or "", "x": (i % 6) * 180, "y": (i // 6) * 160,
                      "category": 0, "table_id": t.id})
    for r in rels:
        key = (r.src_table_id, r.dst_table_id)
        if key in seen_e:
            continue
        seen_e.add(key)
        edges.append({"source": f"t{r.src_table_id}", "target": f"t{r.dst_table_id}",
                      "label": r.rel_type})
    return {"nodes": nodes, "edges": edges}
