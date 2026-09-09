"""C8 血缘 API：查询血缘图 + 触发挖掘。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..engine.lineage import get_lineage, mine_query_logs, mine_view_ddl
from ..models import Datasource, TableMeta
from .deps import get_current_user

router = APIRouter(prefix="/lineage", tags=["lineage"])


@router.get("/{table_id}")
def lineage_detail(table_id: int, direction: str = "both",
                   db: Session = Depends(get_db), user=Depends(get_current_user)):
    """血缘图：upstream / downstream / both。"""
    if direction not in ("upstream", "downstream", "both"):
        raise HTTPException(400, "direction 取值 upstream/downstream/both")
    tm = db.query(TableMeta).get(table_id)
    if not tm:
        raise HTTPException(404, "表不存在")
    return get_lineage(db, user.workspace_id, table_id, direction)


@router.post("/mine")
def trigger_mine(datasource_id: int | None = None,
                 db: Session = Depends(get_db), user=Depends(get_current_user)):
    """触发血缘挖掘：QueryLog 关联边 + 视图 DDL 边。"""
    ds_list = []
    if datasource_id:
        ds = db.query(Datasource).get(datasource_id)
        if not ds:
            raise HTTPException(404, "数据源不存在")
        ds_list = [ds]
    else:
        ds_list = (db.query(Datasource)
                   .filter(Datasource.workspace_id == user.workspace_id,
                           Datasource.status == "ok").all())
    stats = {"query_edges": 0, "ddl_edges": 0, "datasources": len(ds_list)}
    for ds in ds_list:
        try:
            stats["query_edges"] += mine_query_logs(ds.id)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"QueryLog 挖掘失败: {exc}") from exc
        try:
            stats["ddl_edges"] += mine_view_ddl(ds.id)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"视图挖掘失败: {exc}") from exc
    return {"ok": True, "stats": stats}
