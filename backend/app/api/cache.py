"""C1 缓存管理 API：统计与清空。"""
from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..engine.cache_manager import invalidate_all_for_workspace, invalidate_for_datasource
from ..models import CacheEntry
from .deps import get_current_user

router = APIRouter(prefix="/cache", tags=["cache"])


@router.get("/stats")
def cache_stats(db: Session = Depends(get_db), user=Depends(get_current_user)):
    """当前工作空间缓存统计（类型分布/命中情况/容量）。"""
    q = db.query(CacheEntry).filter(CacheEntry.workspace_id == user.workspace_id)
    total = q.count()
    by_type = dict(
        db.query(CacheEntry.cache_type, func.count(CacheEntry.id))
        .filter(CacheEntry.workspace_id == user.workspace_id)
        .group_by(CacheEntry.cache_type).all())
    total_hits = db.query(func.coalesce(func.sum(CacheEntry.hit_count), 0)) \
        .filter(CacheEntry.workspace_id == user.workspace_id).scalar() or 0
    recent = (q.order_by(CacheEntry.last_hit_at.desc())
              .limit(20).all())
    items = [{"id": c.id, "cache_type": c.cache_type, "cache_key": c.cache_key,
              "question": c.question, "sql_text": (c.sql_text or "")[:200],
              "datasource_id": c.datasource_id, "hit_count": c.hit_count or 0,
              "last_hit_at": c.last_hit_at, "expires_at": c.expires_at}
             for c in recent]
    return {"total": total, "by_type": by_type, "total_hits": total_hits,
            "recent": items}


@router.delete("")
def clear_cache(datasource_id: int | None = None, cache_type: str | None = None,
                db: Session = Depends(get_db), user=Depends(get_current_user)):
    """清空缓存：cache_type（gen/result）按类型 / datasource_id 按数据源 / 都不传清空整个工作空间。"""
    if cache_type:
        n = db.query(CacheEntry).filter(
            CacheEntry.workspace_id == user.workspace_id,
            CacheEntry.cache_type == cache_type).delete(synchronize_session=False)
    elif datasource_id:
        n = invalidate_for_datasource(datasource_id, db)
    else:
        n = invalidate_all_for_workspace(user.workspace_id, db)
    db.commit()
    return {"ok": True, "cleared": n}
