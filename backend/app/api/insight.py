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
    model_id: int | None = None


class ExecuteIn(BaseModel):
    config: dict
    template_id: int | None = None
    prompt_template_id: int | None = None


class PreviewIn(BaseModel):
    config: dict


class SaveTemplateIn(BaseModel):
    config: dict
    name: str
    description: str = ""
    prompt_template_id: int | None = None


@router.post("/generate-draft")
def generate_draft(body: DraftIn, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """根据分析目的 + 数据源生成分析项草案。"""
    from ..engine.llm_provider import resolve_llm_client, resolve_llm_client_by_id
    if body.model_id:
        llm = resolve_llm_client_by_id(db, body.model_id, user.workspace_id)
    else:
        llm = resolve_llm_client(db, user.workspace_id, scene="sql")
    gen = InsightGenerator(db, llm=llm, workspace_id=user.workspace_id)
    config = gen.generate_draft(body.purpose, body.datasource_id, model_id=body.model_id)
    return {"ok": True, "config": config.to_dict()}


@router.post("/preview")
def preview_report(body: PreviewIn, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """预览：执行各分析项 SQL，返回卡片数据（不保存报告）。"""
    from ..engine.llm_provider import resolve_llm_client, resolve_llm_client_by_id
    config = InsightConfig.from_dict(body.config)
    if config.model_id:
        llm = resolve_llm_client_by_id(db, config.model_id, user.workspace_id)
    else:
        llm = resolve_llm_client(db, user.workspace_id, scene="sql")
    gen = InsightGenerator(db, llm=llm, workspace_id=user.workspace_id)
    cards = gen.preview(config, user_id=user.id)
    return {"ok": True, "cards": cards}


@router.post("/execute")
def execute_insight(body: ExecuteIn, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """执行配置，生成完整报告（写入 report 表统一管理）。"""
    from ..engine.llm_provider import resolve_llm_client, resolve_llm_client_by_id
    config = InsightConfig.from_dict(body.config)
    if config.model_id:
        llm = resolve_llm_client_by_id(db, config.model_id, user.workspace_id)
    else:
        llm = resolve_llm_client(db, user.workspace_id, scene="sql")
    gen = InsightGenerator(db, llm=llm, workspace_id=user.workspace_id)
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
                                     "config": t.config, "created_at": t.create_time.isoformat() if t.create_time else None}
                                    for t in items]}


@router.put("/templates/{template_id}")
def update_template(template_id: int, body: SaveTemplateIn,
                    db: Session = Depends(get_db), user=Depends(get_current_user)):
    """更新模板配置。"""
    tpl = db.query(InsightTemplate).filter(InsightTemplate.id == template_id,
                                             InsightTemplate.is_deleted.is_(False)).first()
    if not tpl or tpl.workspace_id != user.workspace_id:
        raise HTTPException(404, "模板不存在")
    config = InsightConfig.from_dict(body.config)
    tpl.name = body.name
    tpl.description = body.description or ""
    tpl.purpose = config.purpose
    tpl.datasource_id = config.datasource_id
    tpl.config = config.to_dict()
    if body.prompt_template_id is not None:
        tpl.prompt_template_id = body.prompt_template_id
    db.commit()
    db.refresh(tpl)
    return {"ok": True, "template_id": tpl.id}


@router.delete("/templates/{template_id}")
def delete_template(template_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    tpl = db.query(InsightTemplate).filter(InsightTemplate.id == template_id,
                                             InsightTemplate.is_deleted.is_(False)).first()
    if not tpl or tpl.workspace_id != user.workspace_id:
        raise HTTPException(404, "模板不存在")
    tpl.is_deleted = True
    db.commit()
    return {"ok": True}
