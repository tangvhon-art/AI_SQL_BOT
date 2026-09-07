"""API 依赖：DB 会话、当前用户。"""
from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db, set_current_user
from ..models import User
from ..security import decode_access_token


def get_current_user(authorization: str = Header(default=""),
                     db: Session = Depends(get_db)) -> User:
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "未登录")
    token = authorization.split(" ", 1)[1]
    try:
        payload = decode_access_token(token)
        user = db.query(User).get(int(payload["sub"]))
    except Exception:  # noqa: BLE001
        raise HTTPException(401, "登录已过期") from None
    if not user:
        raise HTTPException(401, "用户不存在")
    set_current_user(user.id)  # 供审计字段 create_user/update_user 自动填充
    return user
