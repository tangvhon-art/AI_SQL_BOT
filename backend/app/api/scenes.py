"""C14 场景模板 API：场景列表 / 详情 / 自定义 CRUD。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..engine.scene_router import detect_scene, get_scene_context
from ..models import SceneDef
from .deps import get_current_user

router = APIRouter(prefix="/scenes", tags=["scenes"])


class SceneIn(BaseModel):
    scene_code: str
    scene_name: str
    description: str = ""
    metric_pack: list[dict] = []
    gen_prompt_template: str = ""
    explain_template: str = ""
    examples: list[dict] = []
    report_template: str = ""
    enabled: bool = True
    sort_order: int = 0


def _out(s: SceneDef) -> dict:
    return {"id": s.id, "workspace_id": s.workspace_id, "scene_code": s.scene_code,
            "scene_name": s.scene_name, "description": s.description or "",
            "metric_pack": s.metric_pack_json or [],
            "gen_prompt_template": s.gen_prompt_template or "",
            "explain_template": s.explain_template or "",
            "examples": s.examples_json or [],
            "report_template": s.report_template or "",
            "enabled": s.enabled, "sort_order": s.sort_order,
            "system": s.workspace_id is None}


@router.get("")
def list_scenes(db: Session = Depends(get_db), user=Depends(get_current_user)):
    """场景列表：系统预置 + 当前工作空间自定义（自定义优先展示）。"""
    rows = (db.query(SceneDef)
            .filter((SceneDef.workspace_id.is_(None)) |
                    (SceneDef.workspace_id == user.workspace_id))
            .order_by(SceneDef.sort_order.asc(), SceneDef.id.asc()).all())
    return {"items": [_out(s) for s in rows]}


@router.get("/detect")
def scene_detect(question: str, db: Session = Depends(get_db),
                 user=Depends(get_current_user)):
    """场景识别：返回命中场景码与提示词上下文。"""
    code = detect_scene(question, db)
    return {"scene_code": code,
            "context": get_scene_context(code, user.workspace_id, db) if code else ""}


@router.post("")
def create_scene(body: SceneIn, db: Session = Depends(get_db),
                 user=Depends(get_current_user)):
    exists = (db.query(SceneDef)
              .filter(SceneDef.workspace_id == user.workspace_id,
                      SceneDef.scene_code == body.scene_code).first())
    if exists:
        raise HTTPException(400, "该工作空间已存在同编码场景，请修改或直接更新")
    s = SceneDef(workspace_id=user.workspace_id, scene_code=body.scene_code,
                 scene_name=body.scene_name, description=body.description,
                 metric_pack_json=body.metric_pack,
                 gen_prompt_template=body.gen_prompt_template,
                 explain_template=body.explain_template,
                 examples_json=body.examples,
                 report_template=body.report_template,
                 enabled=body.enabled, sort_order=body.sort_order)
    db.add(s)
    db.commit()
    db.refresh(s)
    return _out(s)


@router.put("/{scene_id}")
def update_scene(scene_id: int, body: SceneIn, db: Session = Depends(get_db),
                 user=Depends(get_current_user)):
    s = db.query(SceneDef).get(scene_id)
    if not s or s.workspace_id != user.workspace_id:
        raise HTTPException(404, "场景不存在或不可编辑（系统预置只读）")
    s.scene_name = body.scene_name
    s.description = body.description
    s.metric_pack_json = body.metric_pack
    s.gen_prompt_template = body.gen_prompt_template
    s.explain_template = body.explain_template
    s.examples_json = body.examples
    s.report_template = body.report_template
    s.enabled = body.enabled
    s.sort_order = body.sort_order
    db.commit()
    return _out(s)


@router.delete("/{scene_id}")
def delete_scene(scene_id: int, db: Session = Depends(get_db),
                 user=Depends(get_current_user)):
    s = db.query(SceneDef).get(scene_id)
    if not s or s.workspace_id != user.workspace_id:
        raise HTTPException(404, "场景不存在或不可删除（系统预置只读）")
    s.is_deleted = True
    db.commit()
    return {"ok": True}
