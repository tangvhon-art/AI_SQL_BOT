"""报告管理 API：保存/查询/删除问数报告（Dashboard + AI 解读快照）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Report
from .deps import get_current_user

router = APIRouter(prefix="/reports", tags=["报告管理"])


class ReportIn(BaseModel):
    title: str
    original_question: str = ""
    multi_query_spec: dict = {}
    dashboard_data: dict = {}
    ai_interpretation: dict = {}
    interpretation_text: str = ""
    remark: str = ""


class ReportUpdate(BaseModel):
    title: str | None = None
    remark: str | None = None


@router.get("")
def list_reports(keyword: str = "", page: int = 1, page_size: int = 20,
                 db: Session = Depends(get_db), user=Depends(get_current_user)):
    q = db.query(Report).filter(
        Report.workspace_id == user.workspace_id,
        Report.is_deleted.is_(False),
    )
    if keyword:
        q = q.filter(Report.title.contains(keyword))
    total = q.count()
    items = q.order_by(Report.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return {"ok": True, "total": total, "page": page, "page_size": page_size,
            "items": [_to_dict(p, detail=False) for p in items]}


@router.get("/{report_id}")
def get_report(report_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    p = db.query(Report).filter(Report.id == report_id, Report.is_deleted.is_(False)).first()
    if not p or p.workspace_id != user.workspace_id:
        raise HTTPException(404, "报告不存在")
    return {"ok": True, "item": _to_dict(p, detail=True)}


@router.post("")
def create_report(body: ReportIn, db: Session = Depends(get_db), user=Depends(get_current_user)):
    p = Report(
        workspace_id=user.workspace_id,
        title=body.title,
        original_question=body.original_question,
        multi_query_spec=body.multi_query_spec,
        dashboard_data=body.dashboard_data,
        ai_interpretation=body.ai_interpretation,
        interpretation_text=body.interpretation_text,
        remark=body.remark,
        created_by=user.id,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return {"ok": True, "item": _to_dict(p, detail=True)}


@router.put("/{report_id}")
def update_report(report_id: int, body: ReportUpdate,
                  db: Session = Depends(get_db), user=Depends(get_current_user)):
    p = db.query(Report).filter(Report.id == report_id, Report.is_deleted.is_(False)).first()
    if not p or p.workspace_id != user.workspace_id:
        raise HTTPException(404, "报告不存在")
    if body.title is not None:
        p.title = body.title
    if body.remark is not None:
        p.remark = body.remark
    db.commit()
    return {"ok": True}


@router.delete("/{report_id}")
def delete_report(report_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    p = db.query(Report).filter(Report.id == report_id, Report.is_deleted.is_(False)).first()
    if not p or p.workspace_id != user.workspace_id:
        raise HTTPException(404, "报告不存在")
    p.is_deleted = True
    db.commit()
    return {"ok": True}


def _to_dict(p: Report, detail: bool = False) -> dict:
    d = {
        "id": p.id,
        "title": p.title,
        "original_question": p.original_question,
        "remark": p.remark,
        "created_by": p.created_by,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }
    if detail:
        d["multi_query_spec"] = p.multi_query_spec or {}
        d["dashboard_data"] = p.dashboard_data or {}
        d["ai_interpretation"] = p.ai_interpretation or {}
        d["interpretation_text"] = p.interpretation_text or ""
    return d
