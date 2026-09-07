"""意图词典/句式模板管理（AI 问数重构：L2 意图理解层配置化）。

- intent_dict：指标/维度/时间/条件/动作五类词典（term → 目标表/字段/聚合/单位）
- intent_template：句式模板（pattern + intent + slot_map）
支持按工作空间隔离；管理员维护，普通用户只读（走问数链路隐式生效）。
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import IntentDict, IntentTemplate
from .common import get_owned_or_404, workspace_scope
from .deps import get_current_user

router = APIRouter(tags=["dicts"])
logger = logging.getLogger(__name__)

DICT_TYPES = ("metric", "dimension", "time", "filter", "action")
INTENTS = ("value", "compare", "ranking", "trend", "detail", "statistic")


class IntentDictIn(BaseModel):
    dict_type: str
    term: str
    aliases: list[str] = []
    target_table: str = ""
    target_column: str = ""
    agg: str = ""
    unit: str = ""
    enabled: bool = True


class IntentTemplateIn(BaseModel):
    pattern: str
    intent: str
    slot_map: dict = {}
    priority: int = 0
    enabled: bool = True


# ---------- 词典 ----------
@router.get("/dicts")
def list_dicts(dict_type: str | None = None, db: Session = Depends(get_db),
               user=Depends(get_current_user)):
    q = workspace_scope(db, IntentDict, user)
    if dict_type:
        q = q.filter(IntentDict.dict_type == dict_type)
    rows = q.order_by(IntentDict.dict_type, IntentDict.id.desc()).limit(500).all()
    return [{"id": r.id, "dict_type": r.dict_type, "term": r.term, "aliases": r.aliases or [],
             "target_table": r.target_table, "target_column": r.target_column,
             "agg": r.agg, "unit": r.unit, "enabled": r.enabled,
             "create_time": r.create_time} for r in rows]


@router.post("/dicts")
def create_dict(body: IntentDictIn, db: Session = Depends(get_db),
                user=Depends(get_current_user)):
    if body.dict_type not in DICT_TYPES:
        raise HTTPException(400, f"dict_type 必须为 {'/'.join(DICT_TYPES)}")
    if not body.term.strip():
        raise HTTPException(400, "词条不能为空")
    d = IntentDict(workspace_id=user.workspace_id, dict_type=body.dict_type,
                   term=body.term.strip(), aliases=body.aliases,
                   target_table=body.target_table, target_column=body.target_column,
                   agg=body.agg, unit=body.unit, enabled=body.enabled)
    db.add(d)
    db.commit()
    db.refresh(d)
    return {"id": d.id}


@router.put("/dicts/{dict_id}")
def update_dict(dict_id: int, body: IntentDictIn, db: Session = Depends(get_db),
                user=Depends(get_current_user)):
    d = get_owned_or_404(db, IntentDict, dict_id, user, "词条不存在")
    d.dict_type = body.dict_type
    d.term = body.term.strip()
    d.aliases = body.aliases
    d.target_table = body.target_table
    d.target_column = body.target_column
    d.agg = body.agg
    d.unit = body.unit
    d.enabled = body.enabled
    db.commit()
    return {"ok": True}


@router.delete("/dicts/{dict_id}")
def delete_dict(dict_id: int, db: Session = Depends(get_db),
                user=Depends(get_current_user)):
    d = get_owned_or_404(db, IntentDict, dict_id, user, "词条不存在")
    db.delete(d)
    db.commit()
    return {"ok": True}


# ---------- 句式模板 ----------
@router.get("/intent-templates")
def list_templates(db: Session = Depends(get_db), user=Depends(get_current_user)):
    rows = (db.query(IntentTemplate)
            .filter(IntentTemplate.workspace_id == user.workspace_id)
            .order_by(IntentTemplate.priority.desc(), IntentTemplate.id.desc()).all())
    return [{"id": r.id, "pattern": r.pattern, "intent": r.intent,
             "slot_map": r.slot_map or {}, "priority": r.priority,
             "enabled": r.enabled, "create_time": r.create_time} for r in rows]


@router.post("/intent-templates")
def create_template(body: IntentTemplateIn, db: Session = Depends(get_db),
                    user=Depends(get_current_user)):
    if body.intent not in INTENTS:
        raise HTTPException(400, f"intent 必须为 {'/'.join(INTENTS)}")
    if not body.pattern.strip():
        raise HTTPException(400, "模板不能为空")
    t = IntentTemplate(workspace_id=user.workspace_id, pattern=body.pattern.strip(),
                       intent=body.intent, slot_map=body.slot_map,
                       priority=body.priority, enabled=body.enabled)
    db.add(t)
    db.commit()
    db.refresh(t)
    return {"id": t.id}


@router.put("/intent-templates/{tpl_id}")
def update_template(tpl_id: int, body: IntentTemplateIn, db: Session = Depends(get_db),
                    user=Depends(get_current_user)):
    t = db.query(IntentTemplate).get(tpl_id)
    if not t or t.workspace_id != user.workspace_id:
        raise HTTPException(404, "模板不存在")
    t.pattern = body.pattern.strip()
    t.intent = body.intent
    t.slot_map = body.slot_map
    t.priority = body.priority
    t.enabled = body.enabled
    db.commit()
    return {"ok": True}


@router.delete("/intent-templates/{tpl_id}")
def delete_template(tpl_id: int, db: Session = Depends(get_db),
                    user=Depends(get_current_user)):
    t = db.query(IntentTemplate).get(tpl_id)
    if not t or t.workspace_id != user.workspace_id:
        raise HTTPException(404, "模板不存在")
    db.delete(t)
    db.commit()
    return {"ok": True}
