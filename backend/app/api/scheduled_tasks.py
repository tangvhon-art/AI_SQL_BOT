"""保存查询与定时任务：CRUD / 手动执行 / 运行历史。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..engine.scheduler import parse_cron, run_task
from ..models import SavedQuery, ScheduledTask, TaskRunLog
from .common import get_or_404, soft_delete, workspace_scope
from .deps import get_current_user

router = APIRouter(tags=["saved"])


# ---------- 保存查询 ----------
class SavedQueryIn(BaseModel):
    name: str
    sql_text: str = ""
    params: list[dict] = []
    chart_config: dict = {}
    tags: str = ""
    remark: str = ""


def _sq_out(sq: SavedQuery) -> dict:
    return {"id": sq.id, "name": sq.name, "sql_text": sq.sql_text,
            "params": sq.params_json or [], "chart_config": sq.chart_config_json or {},
            "tags": sq.tags, "remark": sq.remark, "owner_id": sq.owner_id}


@router.get("/saved-queries")
def list_saved(db: Session = Depends(get_db), user=Depends(get_current_user)):
    rows = workspace_scope(db, SavedQuery, user).order_by(SavedQuery.id.desc()).all()
    return [_sq_out(sq) for sq in rows]


@router.post("/saved-queries")
def create_saved(body: SavedQueryIn, db: Session = Depends(get_db),
                 user=Depends(get_current_user)):
    if not body.sql_text:
        raise HTTPException(400, "SQL 不能为空")
    sq = SavedQuery(workspace_id=user.workspace_id, owner_id=user.id, name=body.name,
                    sql_text=body.sql_text, params_json=body.params,
                    chart_config_json=body.chart_config, tags=body.tags, remark=body.remark)
    db.add(sq)
    db.commit()
    db.refresh(sq)
    return _sq_out(sq)


@router.put("/saved-queries/{sq_id}")
def update_saved(sq_id: int, body: SavedQueryIn, db: Session = Depends(get_db),
                 user=Depends(get_current_user)):
    sq = get_or_404(db, SavedQuery, sq_id, "保存查询不存在")
    for f in ("name", "sql_text", "params", "chart_config", "tags", "remark"):
        setattr(sq, f, getattr(body, f))
    db.commit()
    return _sq_out(sq)


@router.delete("/saved-queries/{sq_id}")
def delete_saved(sq_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    sq = db.query(SavedQuery).get(sq_id)
    if sq:
        sq.is_deleted = True
        db.commit()
    return {"ok": True}


# ---------- 定时任务 ----------
class TaskIn(BaseModel):
    saved_query_id: int
    name: str
    cron_expr: str = "daily 09:00"
    timezone: str = "Asia/Shanghai"
    param_values: dict = {}
    status: str = "enabled"


def _task_out(t: ScheduledTask) -> dict:
    return {"id": t.id, "saved_query_id": t.saved_query_id, "name": t.name,
            "cron_expr": t.cron_expr, "timezone": t.timezone,
            "param_values": t.param_values_json or {}, "status": t.status,
            "last_run_at": t.last_run_at, "next_run_at": t.next_run_at}


@router.get("/scheduled-tasks")
def list_tasks(db: Session = Depends(get_db), user=Depends(get_current_user)):
    rows = (db.query(ScheduledTask)
            .filter(ScheduledTask.saved_query_id.in_(
                db.query(SavedQuery.id).filter(SavedQuery.workspace_id == user.workspace_id)))
            .order_by(ScheduledTask.id.desc()).all())
    out = []
    for t in rows:
        item = _task_out(t)
        sq = db.query(SavedQuery).get(t.saved_query_id)
        item["saved_query_name"] = sq.name if sq else ""
        out.append(item)
    return out


@router.post("/scheduled-tasks")
def create_task(body: TaskIn, db: Session = Depends(get_db), user=Depends(get_current_user)):
    try:
        nxt = parse_cron(body.cron_expr)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"cron 表达式非法: {exc}") from exc
    sq = db.query(SavedQuery).get(body.saved_query_id)
    if not sq:
        raise HTTPException(404, "保存查询不存在")
    t = ScheduledTask(saved_query_id=body.saved_query_id, name=body.name,
                      cron_expr=body.cron_expr, timezone=body.timezone,
                      param_values_json=body.param_values, status=body.status,
                      next_run_at=nxt)
    db.add(t)
    db.commit()
    db.refresh(t)
    return _task_out(t)


@router.put("/scheduled-tasks/{task_id}")
def update_task(task_id: int, body: TaskIn, db: Session = Depends(get_db),
                user=Depends(get_current_user)):
    t = get_or_404(db, ScheduledTask, task_id, "任务不存在")
    try:
        nxt = parse_cron(body.cron_expr)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"cron 表达式非法: {exc}") from exc
    t.saved_query_id = body.saved_query_id
    t.name = body.name
    t.cron_expr = body.cron_expr
    t.timezone = body.timezone
    t.param_values_json = body.param_values
    t.status = body.status
    t.next_run_at = nxt
    db.commit()
    return _task_out(t)


@router.delete("/scheduled-tasks/{task_id}")
def delete_task(task_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    t = db.query(ScheduledTask).get(task_id)
    if t:
        soft_delete(db, t, db.query(TaskRunLog).filter(TaskRunLog.task_id == task_id))
    return {"ok": True}


@router.post("/scheduled-tasks/{task_id}/run")
def run_now(task_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    t = get_or_404(db, ScheduledTask, task_id, "任务不存在")
    result = run_task(task_id)
    return result


@router.get("/scheduled-tasks/{task_id}/runs")
def task_runs(task_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    rows = (db.query(TaskRunLog).filter(TaskRunLog.task_id == task_id)
            .order_by(TaskRunLog.id.desc()).limit(50).all())
    return [{"id": r.id, "run_time": r.run_time, "status": r.status,
             "param_values": r.param_values_json or {}, "row_count": r.row_count,
             "latency_ms": r.latency_ms, "error_msg": r.error_msg,
             "chart_snapshot": r.chart_snapshot_json or {}} for r in rows]
