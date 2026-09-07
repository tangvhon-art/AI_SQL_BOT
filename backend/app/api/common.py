"""API 层公共调用方法：统一 404 查询、工作空间归属校验、批量赋值、逻辑删除与工作空间过滤。

各路由（datasources / knowledge / models_config / permissions / org / scheduled_tasks / dicts / chat 等）
此前各自重复实现「按主键查 + 判空抛 404」「setattr 批量赋值」「is_deleted=True + 关联软删」等样板逻辑，
统一收敛到本模块，路由只保留业务差异。
"""
from fastapi import HTTPException
from sqlalchemy.orm import Query, Session

from ..database import soft_delete_all


def get_or_404(db: Session, model, obj_id, msg: str = "记录不存在"):
    """按主键查询；对象不存在时抛 404。"""
    obj = db.query(model).get(obj_id)
    if not obj:
        raise HTTPException(404, msg)
    return obj


def get_owned_or_404(db: Session, model, obj_id, user,
                     msg: str = "记录不存在", workspace_field: str = "workspace_id"):
    """按主键查询并校验工作空间归属（当前用户仅能操作本工作空间记录）。

    返回对象；对象不存在或不属于当前用户工作空间时抛 404（避免泄露存在性）。
    """
    obj = db.query(model).get(obj_id)
    if not obj or getattr(obj, workspace_field) != getattr(user, workspace_field):
        raise HTTPException(404, msg)
    return obj


def apply_fields(obj, body, fields: list[str]) -> None:
    """将请求体（pydantic）中指定字段批量赋值到 ORM 对象，保留对象既有其他字段。

    等价于旧样板：``for f in fields: setattr(obj, f, getattr(body, f))``。
    """
    for f in fields:
        if hasattr(body, f):
            setattr(obj, f, getattr(body, f))


def soft_delete(db: Session, obj, *related_queries: Query) -> None:
    """逻辑删除对象及其关联数据并统一提交。

    - related_queries：需要一并软删的关联查询（如子表/关联表，可省略）
    - 主对象置 is_deleted=1；delete_time 由 database 的审计事件自动维护
    """
    for q in related_queries:
        if q is not None:
            soft_delete_all(q)
    obj.is_deleted = True
    db.commit()


def workspace_scope(db: Session, model, user, workspace_field: str = "workspace_id"):
    """返回按当前用户工作空间过滤后的查询对象（可继续链式 filter/order_by）。"""
    return db.query(model).filter(getattr(model, workspace_field) == getattr(user, workspace_field))
