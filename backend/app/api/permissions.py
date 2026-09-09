"""字段级权限规则：CRUD + 生效预览（可查并集/不可查并集/黑名单优先）。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
import logging

logger = logging.getLogger(__name__)
from ..engine.permission import compute_permissions
from ..models import ColumnMeta, Datasource, PermissionRule, Role, TableMeta, User
from .common import get_or_404, paginate, workspace_scope
from .deps import get_current_user

router = APIRouter(prefix="/permission-rules", tags=["permissions"])


class RuleIn(BaseModel):
    scope_type: str  # role / user / group
    scope_id: int
    rule_type: str  # allow / deny
    datasource_id: int
    table_id: int
    column_ids: list[int] = []  # 空数组=整表规则
    enabled: bool = True
    # 行级权限（C3）
    row_filter: str | None = None          # 行过滤条件（SQL 文本或模板文本）
    row_filter_type: str = "sql"           # sql / template
    row_filter_note: str = ""
    row_enabled: bool = False


def _out(r: PermissionRule) -> dict:
    return {"id": r.id, "scope_type": r.scope_type, "scope_id": r.scope_id,
            "rule_type": r.rule_type, "datasource_id": r.datasource_id,
            "table_id": r.table_id, "column_ids": r.column_ids or [], "enabled": r.enabled,
            "row_filter": r.row_filter, "row_filter_type": r.row_filter_type or "sql",
            "row_filter_note": r.row_filter_note or "", "row_enabled": bool(r.row_enabled)}


def _invalidate_cache(ws_id: int, db: Session) -> None:
    """权限规则变更后清空该工作空间缓存（结果可见性可能已变化）。"""
    from ..engine.cache_manager import invalidate_all_for_workspace
    try:
        invalidate_all_for_workspace(ws_id, db)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[cache] 权限变更后缓存失效失败: %s", exc)


def _col_names(db: Session, table_id: int, col_ids: list[int]) -> list[str]:
    """根据字段ID列表查询字段名，空列表或 [0]（整表标记）返回['（整表）']。"""
    effective = [c for c in (col_ids or []) if c != 0]
    if not effective:
        return ["（整表）"]
    cols = db.query(ColumnMeta).filter(ColumnMeta.id.in_(effective)).all()
    id_to_name = {c.id: c.column_name for c in cols}
    return [id_to_name.get(cid, f"未知字段({cid})") for cid in effective]


@router.get("")
def list_rules(scope_type: str = "", rule_type: str = "", datasource_id: int | None = None,
               table: str = "", enabled: str = "", page: int = 1, size: int = 20,
               db: Session = Depends(get_db), user=Depends(get_current_user)):
    query = workspace_scope(db, PermissionRule, user)
    if scope_type:
        query = query.filter(PermissionRule.scope_type == scope_type)
    if rule_type:
        query = query.filter(PermissionRule.rule_type == rule_type)
    if datasource_id:
        query = query.filter(PermissionRule.datasource_id == datasource_id)
    if table:
        table_ids = [t.id for t in db.query(TableMeta)
                     .filter(TableMeta.table_name.like(f"%{table}%")).all()]
        if not table_ids:
            return {"total": 0, "items": []}
        query = query.filter(PermissionRule.table_id.in_(table_ids))
    if enabled in ("1", "0"):
        query = query.filter(PermissionRule.enabled.is_(enabled == "1"))
    query = query.order_by(PermissionRule.id.desc())
    rows, total = paginate(query, page, size)
    out = []
    for r in rows:
        item = _out(r)
        tm = db.query(TableMeta).get(r.table_id)
        ds = db.query(Datasource).get(r.datasource_id)
        item["table_name"] = tm.table_name if tm else ""
        item["column_names"] = _col_names(db, r.table_id, r.column_ids or [])
        item["datasource_name"] = ds.name if ds else ""
        out.append(item)
    return {"total": total, "items": out}


@router.post("")
def create_rule(body: RuleIn, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if body.scope_type not in ("role", "user", "group") or body.rule_type not in ("allow", "deny"):
        raise HTTPException(400, "scope_type/rule_type 非法")
    if body.row_enabled and not (body.row_filter or "").strip():
        raise HTTPException(400, "启用行级规则时 row_filter 不能为空")
    r = PermissionRule(workspace_id=user.workspace_id, **body.model_dump())
    db.add(r)
    db.commit()
    db.refresh(r)
    _invalidate_cache(user.workspace_id, db)
    return _out(r)


@router.put("/{rule_id}")
def update_rule(rule_id: int, body: RuleIn, db: Session = Depends(get_db),
                user=Depends(get_current_user)):
    r = get_or_404(db, PermissionRule, rule_id, "规则不存在")
    for k, v in body.model_dump().items():
        setattr(r, k, v)
    db.commit()
    _invalidate_cache(user.workspace_id, db)
    return _out(r)


@router.delete("/{rule_id}")
def delete_rule(rule_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    r = db.query(PermissionRule).get(rule_id)
    if r:
        r.is_deleted = True
        db.commit()
    _invalidate_cache(user.workspace_id, db)
    return {"ok": True}


@router.get("/effective")
def effective(user_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """给定用户生效的权限视图（可查并集/不可查并集/黑名单优先）。"""
    target = get_or_404(db, User, user_id, "用户不存在")
    allow, deny = compute_permissions(user_id, target.workspace_id)

    def _explain(keys: set[str]) -> list[dict]:
        out = []
        for k in sorted(keys):
            parts = k.split(".")
            if len(parts) == 4:
                out.append({"datasource_id": int(parts[0]), "schema": parts[1],
                            "table": parts[2], "column": parts[3]})
        return out

    return {"user_id": user_id, "allow": _explain(allow), "deny": _explain(deny),
            "rules": _effective_rules(target, user_id, db)}


def _effective_rules(target: User, user_id: int, db: Session) -> list[dict]:
    """该用户命中的所有规则详情（角色规则 + 用户规则），带名称便于前端搜索展示。"""
    from sqlalchemy import or_
    role = db.query(Role).get(target.role_id)
    q = (db.query(PermissionRule)
         .filter(PermissionRule.workspace_id == target.workspace_id)
         .filter(or_(
             (PermissionRule.scope_type == 'role') & (PermissionRule.scope_id == target.role_id),
             (PermissionRule.scope_type == 'user') & (PermissionRule.scope_id == user_id),
         )))
    out = []
    for r in q.all():
        tm = db.query(TableMeta).get(r.table_id)
        ds = db.query(Datasource).get(r.datasource_id)
        scope_name = (role.name if role else str(r.scope_id)) if r.scope_type == 'role' \
            else (target.display_name or target.username)
        col_names = _col_names(db, r.table_id, r.column_ids or [])
        out.append({
            "id": r.id, "scope_type": r.scope_type, "scope_id": r.scope_id,
            "scope_name": scope_name, "rule_type": r.rule_type,
            "datasource_id": r.datasource_id, "datasource_name": ds.name if ds else "",
            "table_id": r.table_id, "table_name": tm.table_name if tm else "",
            "column_ids": r.column_ids or [], "column_name": "、".join(col_names),
            "column_names": col_names,
            "enabled": r.enabled,
            "row_filter": r.row_filter, "row_filter_type": r.row_filter_type or "sql",
            "row_filter_note": r.row_filter_note or "", "row_enabled": bool(r.row_enabled),
        })
    return out


@router.get("/row-effective")
def row_effective(user_id: int, datasource_id: int | None = None,
                  db: Session = Depends(get_db), user=Depends(get_current_user)):
    """行级权限生效预览：给定用户在各表生效的行级规则（deny 优先 / allow 并集）。"""
    from ..engine.permission import compute_row_rules
    from ..models import Datasource

    target = get_or_404(db, User, user_id, "用户不存在")
    ws_id = target.workspace_id
    q = db.query(TableMeta)
    if datasource_id:
        q = q.filter(TableMeta.datasource_id == datasource_id)
    out = []
    for tm in q.order_by(TableMeta.datasource_id, TableMeta.table_name).all():
        rules = compute_row_rules(user_id, ws_id, tm.datasource_id, tm.id)
        if not rules:
            continue
        out.append({
            "datasource_id": tm.datasource_id,
            "table_id": tm.id,
            "table_name": tm.table_name,
            "rules": [{"rule_id": r.rule_id, "rule_type": r.rule_type,
                       "condition": r.condition, "condition_type": r.condition_type,
                       "note": r.note} for r in rules],
        })
    return {"user_id": user_id, "items": out}
