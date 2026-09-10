"""洞察分析 API：生成草案、预览报告、保存模板、模板管理。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..engine.insight_generator import InsightConfig, InsightGenerator
from ..models import InsightTemplate
from .deps import get_current_user

router = APIRouter(prefix="/insight", tags=["洞察分析"])


class DraftIn(BaseModel):
    purpose: str
    datasource_id: int


class ExecuteIn(BaseModel):
    config: dict
    template_id: int | None = None
    prompt_template_id: int | None = None


class SaveTemplateIn(BaseModel):
    config: dict
    name: str
    description: str = ""
    prompt_template_id: int | None = None


@router.post("/generate-draft")
def generate_draft(body: DraftIn, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """根据分析目的 + 数据源生成分析项草案。"""
    from ..engine.llm_provider import resolve_llm_client
    llm = resolve_llm_client(workspace_id=user.workspace_id)
    gen = InsightGenerator(db, llm=llm, workspace_id=user.workspace_id)
    config = gen.generate_draft(body.purpose, body.datasource_id)
    return {"ok": True, "config": config.to_dict()}


@router.post("/execute")
def execute_insight(body: ExecuteIn, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """执行配置，生成完整报告（写入 report 表统一管理）。"""
    from ..engine.llm_provider import resolve_llm_client
    llm = resolve_llm_client(workspace_id=user.workspace_id)
    gen = InsightGenerator(db, llm=llm, workspace_id=user.workspace_id)
    config = InsightConfig.from_dict(body.config)
    report = gen.execute(config, template_id=body.template_id,
                          prompt_template_id=body.prompt_template_id, created_by=user.id)
    return {"ok": True, "report_id": report.id, "title": report.title}


@router.post("/save-template")
def save_template(body: SaveTemplateIn, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """保存配置为模板。"""
    gen = InsightGenerator(db, workspace_id=user.workspace_id)
    config = InsightConfig.from_dict(body.config)
    tpl = gen.save_template(config, body.name, body.description,
                             body.prompt_template_id, created_by=user.id)
    return {"ok": True, "template_id": tpl.id}


@router.get("/templates")
def list_templates(keyword: str = "", db: Session = Depends(get_db), user=Depends(get_current_user)):
    """列出洞察模板。"""
    q = db.query(InsightTemplate).filter(
        InsightTemplate.workspace_id == user.workspace_id,
        InsightTemplate.is_deleted.is_(False),
    )
    if keyword:
        q = q.filter(InsightTemplate.name.contains(keyword))
    items = q.order_by(InsightTemplate.id.desc()).all()
    return {"ok": True, "items": [{"id": t.id, "name": t.name, "description": t.description,
                                     "purpose": t.purpose, "datasource_id": t.datasource_id,
                                     "config": t.config, "created_at": t.created_at.isoformat() if t.created_at else None}
                                    for t in items]}


@router.delete("/templates/{template_id}")
def delete_template(template_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    tpl = db.query(InsightTemplate).filter(InsightTemplate.id == template_id,
                                             InsightTemplate.is_deleted.is_(False)).first()
    if not tpl or tpl.workspace_id != user.workspace_id:
        raise HTTPException(404, "模板不存在")
    tpl.is_deleted = True
    db.commit()
    return {"ok": True}
