"""认证：登录、当前用户信息。初始账号 admin / admin123。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Role, User, Workspace
from ..security import create_access_token, hash_password, verify_password
from .deps import get_current_user

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginIn(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    id: int
    username: str
    display_name: str
    role_code: str
    workspace_id: int

    class Config:
        from_attributes = True


@router.post("/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == body.username).first()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "用户名或密码错误")
    token = create_access_token(user.id, user.username)
    return {"access_token": token, "token_type": "bearer", "user": _to_out(user)}


@router.get("/me")
def me(user: User = Depends(get_current_user)):
    return {"user": _to_out(user)}


def _to_out(user: User) -> dict:
    from ..database import SessionLocal
    from ..models import UserGroup
    from .org import user_group_ids, user_role_ids
    db = SessionLocal()
    try:
        role = db.query(Role).get(user.role_id)
        role_ids = user_role_ids(db, user.id)
        group_ids = user_group_ids(db, user.id)
        groups = (db.query(UserGroup).filter(UserGroup.id.in_(group_ids)).all()
                  if group_ids else [])
        roles = (db.query(Role).filter(Role.id.in_(role_ids)).all()
                 if role_ids else [])
        return {"id": user.id, "username": user.username,
                "display_name": user.display_name,
                "role_code": role.code if role else "biz_user",
                "role_name": role.name if role else "",
                "roles": [{"id": r.id, "code": r.code, "name": r.name} for r in roles],
                "groups": [{"id": g.id, "name": g.name} for g in groups],
                "workspace_id": user.workspace_id}
    finally:
        db.close()


def seed_default_user(db: Session):
    """初始化：默认工作空间 + 三个角色 + admin 用户。
    查询豁免软删过滤：角色/工作空间/admin 若被逻辑删除则复用并恢复（避免唯一约束冲突）。"""
    ws = (db.query(Workspace).execution_options(include_deleted=True).first())
    if not ws:
        ws = Workspace(name="默认工作空间")
        db.add(ws)
        db.flush()
    else:
        ws.is_deleted = False
    roles = {}
    for code, name in (("admin", "管理员"), ("data_admin", "数据管理员"),
                       ("biz_user", "业务用户")):
        r = (db.query(Role).execution_options(include_deleted=True)
             .filter(Role.code == code).first())
        if not r:
            r = Role(code=code, name=name)
            db.add(r)
            db.flush()
        else:
            r.is_deleted = False
        roles[code] = r
    admin = (db.query(User).execution_options(include_deleted=True)
             .filter(User.username == "admin").first())
    if not admin:
        db.add(User(username="admin",
                    password_hash=hash_password("admin123"),
                    role_id=roles["admin"].id,
                    workspace_id=ws.id,
                    display_name="管理员"))
    else:
        admin.is_deleted = False
    db.commit()
