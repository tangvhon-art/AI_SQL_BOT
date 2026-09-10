"""公共 Prompt 管理 API（多场景）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..engine.prompt_service import PromptService
from .deps import get_current_user

router = APIRouter(prefix="/prompts", tags=["Prompt管理"])


class PromptIn(BaseModel):
    scene_type: str = "ai_interpret"
    name: str
    description: str = ""
    scene_tags: str = ""
    prompt_template: str
    is_default: bool = False


class PromptUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    scene_tags: str | None = None
    prompt_template: str | None = None
    is_default: bool | None = None


class TestIn(BaseModel):
    prompt_template: str
    variables: dict = {}


@router.get("")
def list_prompts(scene_type: str | None = None, keyword: str = "",
                 db: Session = Depends(get_db), user=Depends(get_current_user)):
    svc = PromptService(db, workspace_id=user.workspace_id)
    items = svc.list(scene_type=scene_type, keyword=keyword)
    return {"ok": True, "items": [_to_dict(p) for p in items]}


@router.post("")
def create_prompt(body: PromptIn, db: Session = Depends(get_db), user=Depends(get_current_user)):
    svc = PromptService(db, workspace_id=user.workspace_id)
    p = svc.create(
        scene_type=body.scene_type, name=body.name,
        prompt_template=body.prompt_template, description=body.description,
        scene_tags=body.scene_tags, is_default=body.is_default,
        created_by=user.id,
    )
    return {"ok": True, "item": _to_dict(p)}


@router.put("/{prompt_id}")
def update_prompt(prompt_id: int, body: PromptUpdate,
                  db: Session = Depends(get_db), user=Depends(get_current_user)):
    svc = PromptService(db, workspace_id=user.workspace_id)
    p = svc.update(prompt_id, **body.model_dump(exclude_none=True))
    if not p:
        raise HTTPException(404, "模板不存在")
    return {"ok": True, "item": _to_dict(p)}


@router.delete("/{prompt_id}")
def delete_prompt(prompt_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    svc = PromptService(db, workspace_id=user.workspace_id)
    if not svc.delete(prompt_id):
        raise HTTPException(404, "模板不存在")
    return {"ok": True}


@router.post("/{prompt_id}/set-default")
def set_default(prompt_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    svc = PromptService(db, workspace_id=user.workspace_id)
    if not svc.set_default(prompt_id):
        raise HTTPException(404, "模板不存在")
    return {"ok": True}


@router.post("/test")
def test_render(body: TestIn):
    """测试 Prompt 渲染：返回渲染后的文本和提取的变量列表。"""
    rendered = PromptService.render(body.prompt_template, body.variables)
    variables = PromptService.extract_variables(body.prompt_template)
    return {"ok": True, "rendered": rendered, "variables": variables}


@router.get("/scene-types")
def list_scene_types():
    """列出支持的场景类型。"""
    return {"ok": True, "items": [
        {"value": "ai_interpret", "label": "AI解读"},
        {"value": "insight_draft", "label": "洞察草案"},
        {"value": "sql_generation", "label": "SQL生成"},
        {"value": "custom", "label": "自定义"},
    ]}


def _to_dict(p) -> dict:
    return {
        "id": p.id,
        "workspace_id": p.workspace_id,
        "scene_type": p.scene_type,
        "name": p.name,
        "description": p.description,
        "scene_tags": p.scene_tags,
        "prompt_template": p.prompt_template,
        "output_format": p.output_format,
        "is_default": p.is_default,
        "is_builtin": p.is_builtin,
        "sort_order": p.sort_order,
        "created_by": p.created_by,
        "created_at": p.create_time.isoformat() if p.create_time else None,
    }
