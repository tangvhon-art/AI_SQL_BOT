"""C4 评测闭环 API：用例 CRUD + 批次发起/查询。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..engine.evaluator import start_batch
from ..models import Datasource, EvalCase, EvalResult, EvalRun
from .common import paginate, workspace_scope
from .deps import get_current_user

router = APIRouter(prefix="/eval", tags=["eval"])


class EvalCaseIn(BaseModel):
    datasource_id: int
    question: str
    expect_tables: list[str] = []
    expect_metrics: list[str] = []
    expect_filters: list[str] = []
    expect_sql: str = ""
    scene_code: str = ""
    tags: str = ""


class EvalRunIn(BaseModel):
    name: str = ""
    case_ids: list[int] = []
    tags: list[str] = []
    mock_execute: bool | None = None  # None=取配置默认


def _case_out(c: EvalCase) -> dict:
    return {"id": c.id, "datasource_id": c.datasource_id, "question": c.question,
            "expect_tables": c.expect_tables_json or [],
            "expect_metrics": c.expect_metrics_json or [],
            "expect_filters": c.expect_filters_json or [],
            "expect_sql": c.expect_sql, "scene_code": c.scene_code,
            "tags": c.tags, "status": c.status}


# ---------- 用例 ----------

@router.get("/cases")
def list_cases(datasource_id: int | None = None, scene_code: str = "",
               page: int = 1, size: int = 20,
               db: Session = Depends(get_db), user=Depends(get_current_user)):
    q = workspace_scope(db, EvalCase, user)
    if datasource_id:
        q = q.filter(EvalCase.datasource_id == datasource_id)
    if scene_code:
        q = q.filter(EvalCase.scene_code == scene_code)
    q = q.order_by(EvalCase.id.desc())
    rows, total = paginate(q, page, size)
    out = []
    for c in rows:
        d = _case_out(c)
        ds = db.query(Datasource).get(c.datasource_id)
        d["datasource_name"] = ds.name if ds else ""
        out.append(d)
    return {"total": total, "items": out}


@router.post("/cases")
def create_case(body: EvalCaseIn, db: Session = Depends(get_db),
                user=Depends(get_current_user)):
    if not body.question.strip():
        raise HTTPException(400, "question 不能为空")
    c = EvalCase(workspace_id=user.workspace_id, datasource_id=body.datasource_id,
                 question=body.question.strip(),
                 expect_tables_json=body.expect_tables,
                 expect_metrics_json=body.expect_metrics,
                 expect_filters_json=body.expect_filters,
                 expect_sql=body.expect_sql, scene_code=body.scene_code,
                 tags=body.tags, created_by=user.id)
    db.add(c)
    db.commit()
    db.refresh(c)
    return _case_out(c)


@router.delete("/cases/{case_id}")
def delete_case(case_id: int, db: Session = Depends(get_db),
                user=Depends(get_current_user)):
    c = db.query(EvalCase).get(case_id)
    if c:
        c.is_deleted = True
        db.commit()
    return {"ok": True}


# ---------- 批次 ----------

@router.get("/runs")
def list_runs(page: int = 1, size: int = 10,
              db: Session = Depends(get_db), user=Depends(get_current_user)):
    q = workspace_scope(db, EvalRun, user).order_by(EvalRun.id.desc())
    rows, total = paginate(q, page, size)
    return {"total": total, "items": [
        {"id": r.id, "name": r.name, "status": r.status, "total": r.total,
         "metrics": r.metrics_json or {}, "started_at": r.started_at,
         "finished_at": r.finished_at, "mock_execute": r.mock_execute}
        for r in rows]}


@router.post("/runs")
def create_run(body: EvalRunIn, db: Session = Depends(get_db),
               user=Depends(get_current_user)):
    from ..config import get_settings
    from ..engine.llm_provider import resolve_llm_client

    mock = body.mock_execute if body.mock_execute is not None \
        else get_settings().eval_mock_execute
    run = EvalRun(workspace_id=user.workspace_id, name=body.name or "评测批次",
                  scope_json={"case_ids": body.case_ids, "tags": body.tags},
                  status="pending", mock_execute=mock, created_by=user.id)
    db.add(run)
    db.commit()
    db.refresh(run)
    llm = resolve_llm_client(db, user.workspace_id, scene="sql")
    start_batch(run.id, llm)
    return {"id": run.id, "status": run.status, "mock_execute": run.mock_execute}


@router.get("/runs/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db),
            user=Depends(get_current_user)):
    run = db.query(EvalRun).get(run_id)
    if not run:
        raise HTTPException(404, "批次不存在")
    results = (db.query(EvalResult)
               .filter(EvalResult.run_id == run_id)
               .order_by(EvalResult.id.asc()).all())
    items = []
    for r in results:
        case = db.query(EvalCase).get(r.case_id)
        items.append({
            "case_id": r.case_id,
            "question": case.question if case else "",
            "intent_ok": r.intent_ok, "tables_hit": r.tables_hit,
            "sql_generated": r.sql_generated, "sql_executable": r.sql_executable,
            "sql_correct": r.sql_correct, "e2e_ok": r.e2e_ok,
            "latency_ms": r.latency_ms, "llm_used": r.llm_used,
            "error_msg": r.error_msg, "detail": r.detail_json or {},
        })
    return {"id": run.id, "name": run.name, "status": run.status,
            "total": run.total, "metrics": run.metrics_json or {},
            "started_at": run.started_at, "finished_at": run.finished_at,
            "mock_execute": run.mock_execute, "results": items}
