"""组织与权限管理：角色管理（分配用户/用户组/菜单）、用户管理、用户组管理、菜单树。
权限口径：用户生效角色 = 用户直接角色 ∪ 用户所属用户组的角色（取并集）。
删除语义：全部为逻辑删除（is_deleted=1），查询侧由全局软删过滤自动排除。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..database import get_db, soft_delete_all
from ..models import (Menu, Role, RoleMenu, RoleUser, RoleUserGroup,
                      User, UserGroup, UserGroupMember)
from ..security import hash_password
from .common import get_or_404, paginate, workspace_scope
from .deps import get_current_user

router = APIRouter(tags=["org"])


# ---------- 通用 ----------
def _role_out(r: Role) -> dict:
    return {"id": r.id, "code": r.code, "name": r.name}


def _user_out(u: User) -> dict:
    return {"id": u.id, "username": u.username, "display_name": u.display_name,
            "status": u.status, "workspace_id": u.workspace_id,
            "role_id": u.role_id}


def _group_out(g: UserGroup) -> dict:
    return {"id": g.id, "name": g.name, "remark": g.remark, "workspace_id": g.workspace_id}


def user_role_ids(db: Session, user_id: int) -> list[int]:
    """用户生效角色 id 列表：直接角色(user.role_id + role_user) ∪ 用户组角色(role_user_group)。"""
    ids: set[int] = set()
    u = db.query(User).get(user_id)
    if u and u.role_id:
        ids.add(u.role_id)
    for ru in db.query(RoleUser).filter(RoleUser.user_id == user_id).all():
        ids.add(ru.role_id)
    group_ids = [m.group_id for m in
                 db.query(UserGroupMember).filter(UserGroupMember.user_id == user_id).all()]
    if group_ids:
        for rg in (db.query(RoleUserGroup)
                   .filter(RoleUserGroup.group_id.in_(group_ids)).all()):
            ids.add(rg.role_id)
    return sorted(ids)


def user_group_ids(db: Session, user_id: int) -> list[int]:
    return [m.group_id for m in
            db.query(UserGroupMember).filter(UserGroupMember.user_id == user_id)
            .order_by(UserGroupMember.id).all()]


# ---------- 菜单 ----------
@router.get("/menus")
def list_menus(db: Session = Depends(get_db), user=Depends(get_current_user)):
    menus = db.query(Menu).order_by(Menu.sort_order, Menu.id).all()
    return [{"id": m.id, "parent_id": m.parent_id, "code": m.code, "name": m.name,
             "path": m.path, "icon": m.icon, "sort_order": m.sort_order} for m in menus]


@router.get("/menus/tree")
def menu_tree(db: Session = Depends(get_db), user=Depends(get_current_user)):
    """按当前用户角色过滤后的菜单树（多级）。"""
    menus = db.query(Menu).order_by(Menu.sort_order, Menu.id).all()
    role_ids = user_role_ids(db, user.id)
    allowed: set[int] = set()
    if user.role_id:
        for rm in db.query(RoleMenu).filter(RoleMenu.role_id == user.role_id).all():
            allowed.add(rm.menu_id)
    if role_ids:
        for rm in db.query(RoleMenu).filter(RoleMenu.role_id.in_(role_ids)).all():
            allowed.add(rm.menu_id)
    visible = [m for m in menus if not allowed or m.id in allowed or m.parent_id == 0]
    tree: list[dict] = []
    for m in visible:
        if m.parent_id == 0:
            tree.append({"id": m.id, "code": m.code, "name": m.name, "path": m.path,
                         "icon": m.icon,
                         "children": [{"id": c.id, "code": c.code, "name": c.name,
                                       "path": c.path, "icon": c.icon}
                                      for c in visible if c.parent_id == m.id]})
    return tree


# ---------- 角色管理 ----------
class RoleIn(BaseModel):
    code: str
    name: str


class IdsIn(BaseModel):
    ids: list[int] = []


@router.get("/roles")
def list_roles(keyword: str = "", page: int = 1, size: int = 20,
               db: Session = Depends(get_db), user=Depends(get_current_user)):
    query = db.query(Role)
    if keyword:
        query = query.filter(or_(Role.code.like(f"%{keyword}%"),
                                 Role.name.like(f"%{keyword}%")))
    query = query.order_by(Role.id)
    rows, total = paginate(query, page, size)
    out = []
    for r in rows:
        item = _role_out(r)
        item["users"] = [u.id for u in db.query(User).filter(User.role_id == r.id).all()]
        item["user_ids"] = [ru.user_id for ru in db.query(RoleUser).filter(RoleUser.role_id == r.id).all()]
        item["group_ids"] = [rg.group_id for rg in db.query(RoleUserGroup).filter(RoleUserGroup.role_id == r.id).all()]
        item["menu_ids"] = [rm.menu_id for rm in db.query(RoleMenu).filter(RoleMenu.role_id == r.id).all()]
        out.append(item)
    return {"total": total, "items": out}


@router.post("/roles")
def create_role(body: RoleIn, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if db.query(Role).filter(Role.code == body.code).first():
        raise HTTPException(400, "角色编码已存在")
    r = Role(code=body.code, name=body.name)
    db.add(r)
    db.commit()
    db.refresh(r)
    return _role_out(r)


@router.put("/roles/{role_id}")
def update_role(role_id: int, body: RoleIn, db: Session = Depends(get_db),
                user=Depends(get_current_user)):
    r = get_or_404(db, Role, role_id, "角色不存在")
    r.code, r.name = body.code, body.name
    db.commit()
    return _role_out(r)


@router.delete("/roles/{role_id}")
def delete_role(role_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    r = db.query(Role).get(role_id)
    if not r:
        return {"ok": True}
    soft_delete_all(db.query(RoleUser).filter(RoleUser.role_id == role_id))
    soft_delete_all(db.query(RoleUserGroup).filter(RoleUserGroup.role_id == role_id))
    soft_delete_all(db.query(RoleMenu).filter(RoleMenu.role_id == role_id))
    r.is_deleted = True
    db.commit()
    return {"ok": True}


@router.put("/roles/{role_id}/users")
def assign_role_users(role_id: int, body: IdsIn, db: Session = Depends(get_db),
                      user=Depends(get_current_user)):
    soft_delete_all(db.query(RoleUser).filter(RoleUser.role_id == role_id))
    for uid in body.ids:
        if db.query(User).get(uid):
            db.add(RoleUser(role_id=role_id, user_id=uid))
    db.commit()
    return {"ok": True}


@router.put("/roles/{role_id}/groups")
def assign_role_groups(role_id: int, body: IdsIn, db: Session = Depends(get_db),
                       user=Depends(get_current_user)):
    soft_delete_all(db.query(RoleUserGroup).filter(RoleUserGroup.role_id == role_id))
    for gid in body.ids:
        if db.query(UserGroup).get(gid):
            db.add(RoleUserGroup(role_id=role_id, group_id=gid))
    db.commit()
    return {"ok": True}


@router.put("/roles/{role_id}/menus")
def assign_role_menus(role_id: int, body: IdsIn, db: Session = Depends(get_db),
                      user=Depends(get_current_user)):
    soft_delete_all(db.query(RoleMenu).filter(RoleMenu.role_id == role_id))
    for mid in body.ids:
        if db.query(Menu).get(mid):
            db.add(RoleMenu(role_id=role_id, menu_id=mid))
    db.commit()
    return {"ok": True}


# ---------- 用户组管理 ----------
class GroupIn(BaseModel):
    name: str
    remark: str = ""


@router.get("/user-groups")
def list_groups(keyword: str = "", page: int = 1, size: int = 20,
                db: Session = Depends(get_db), user=Depends(get_current_user)):
    query = workspace_scope(db, UserGroup, user)
    if keyword:
        query = query.filter(or_(UserGroup.name.like(f"%{keyword}%"),
                                 UserGroup.remark.like(f"%{keyword}%")))
    query = query.order_by(UserGroup.id)
    rows, total = paginate(query, page, size)
    out = []
    for g in rows:
        item = _group_out(g)
        item["member_ids"] = [m.user_id for m in
                              db.query(UserGroupMember).filter(UserGroupMember.group_id == g.id).all()]
        item["role_ids"] = [rg.role_id for rg in
                            db.query(RoleUserGroup).filter(RoleUserGroup.group_id == g.id).all()]
        out.append(item)
    return {"total": total, "items": out}


@router.post("/user-groups")
def create_group(body: GroupIn, db: Session = Depends(get_db), user=Depends(get_current_user)):
    g = UserGroup(workspace_id=user.workspace_id, name=body.name, remark=body.remark)
    db.add(g)
    db.commit()
    db.refresh(g)
    return _group_out(g)


@router.put("/user-groups/{group_id}")
def update_group(group_id: int, body: GroupIn, db: Session = Depends(get_db),
                 user=Depends(get_current_user)):
    g = get_or_404(db, UserGroup, group_id, "用户组不存在")
    g.name, g.remark = body.name, body.remark
    db.commit()
    return _group_out(g)


@router.delete("/user-groups/{group_id}")
def delete_group(group_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    g = db.query(UserGroup).get(group_id)
    if g:
        soft_delete_all(db.query(UserGroupMember).filter(UserGroupMember.group_id == group_id))
        soft_delete_all(db.query(RoleUserGroup).filter(RoleUserGroup.group_id == group_id))
        g.is_deleted = True
        db.commit()
    return {"ok": True}


@router.put("/user-groups/{group_id}/members")
def assign_group_members(group_id: int, body: IdsIn, db: Session = Depends(get_db),
                         user=Depends(get_current_user)):
    soft_delete_all(db.query(UserGroupMember).filter(UserGroupMember.group_id == group_id))
    for uid in body.ids:
        if db.query(User).get(uid):
            db.add(UserGroupMember(group_id=group_id, user_id=uid))
    db.commit()
    return {"ok": True}


# ---------- 用户管理 ----------
class UserIn(BaseModel):
    username: str
    password: str = ""
    display_name: str = ""
    role_id: int = 0
    status: int = 1


@router.get("/users")
def list_users(keyword: str = "", status: int | None = None, role_id: int | None = None,
               page: int = 1, size: int = 20,
               db: Session = Depends(get_db), user=Depends(get_current_user)):
    query = workspace_scope(db, User, user)
    if keyword:
        query = query.filter(or_(User.username.like(f"%{keyword}%"),
                                 User.display_name.like(f"%{keyword}%")))
    if status is not None:
        query = query.filter(User.status == status)
    if role_id:
        # 主角色或直接分配的角色包含该角色的用户都命中
        direct_ids = db.query(RoleUser.user_id).filter(RoleUser.role_id == role_id)
        query = query.filter(or_(User.role_id == role_id, User.id.in_(direct_ids)))
    query = query.order_by(User.id)
    rows, total = paginate(query, page, size)
    out = []
    for u in rows:
        item = _user_out(u)
        role = db.query(Role).get(u.role_id) if u.role_id else None
        item["role_code"] = role.code if role else ""
        item["role_name"] = role.name if role else ""
        item["role_ids"] = user_role_ids(db, u.id)
        item["group_ids"] = user_group_ids(db, u.id)
        item["group_names"] = [g.name for g in
                               db.query(UserGroup).filter(UserGroup.id.in_(item["group_ids"])).all()] if item["group_ids"] else []
        out.append(item)
    return {"total": total, "items": out}


@router.post("/users")
def create_user(body: UserIn, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if db.query(User).filter(User.username == body.username).first():
        raise HTTPException(400, "用户名已存在")
    u = User(username=body.username,
             password_hash=hash_password(body.password or "123456"),
             role_id=body.role_id or 1,
             workspace_id=user.workspace_id,
             display_name=body.display_name or body.username,
             status=body.status)
    db.add(u)
    db.commit()
    db.refresh(u)
    return _user_out(u)


@router.put("/users/{user_id}")
def update_user(user_id: int, body: UserIn, db: Session = Depends(get_db),
                user=Depends(get_current_user)):
    u = get_or_404(db, User, user_id, "用户不存在")
    u.display_name = body.display_name
    u.status = body.status
    if body.role_id:
        u.role_id = body.role_id
    if body.password:
        u.password_hash = hash_password(body.password)
    db.commit()
    return _user_out(u)


@router.delete("/users/{user_id}")
def delete_user(user_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    u = db.query(User).get(user_id)
    if u:
        if u.username == "admin":
            raise HTTPException(400, "内置管理员不可删除")
        soft_delete_all(db.query(RoleUser).filter(RoleUser.user_id == user_id))
        soft_delete_all(db.query(UserGroupMember).filter(UserGroupMember.user_id == user_id))
        u.is_deleted = True
        db.commit()
    return {"ok": True}


@router.put("/users/{user_id}/roles")
def assign_user_roles(user_id: int, body: IdsIn, db: Session = Depends(get_db),
                      user=Depends(get_current_user)):
    soft_delete_all(db.query(RoleUser).filter(RoleUser.user_id == user_id))
    for rid in body.ids:
        if db.query(Role).get(rid):
            db.add(RoleUser(role_id=rid, user_id=user_id))
    db.commit()
    return {"ok": True}


@router.put("/users/{user_id}/groups")
def assign_user_groups(user_id: int, body: IdsIn, db: Session = Depends(get_db),
                       user=Depends(get_current_user)):
    soft_delete_all(db.query(UserGroupMember).filter(UserGroupMember.user_id == user_id))
    for gid in body.ids:
        if db.query(UserGroup).get(gid):
            db.add(UserGroupMember(group_id=gid, user_id=user_id))
    db.commit()
    return {"ok": True}


MENU_SEED = [
    # (code, name, path, icon, parent_code)
    ("chat", "AI 问数", "/chat", "MessageOutlined", None),
    ("data_mgmt", "数据管理", "", "DatabaseOutlined", None),
    ("datasource", "数据源管理", "/datasources", "ApiOutlined", "data_mgmt"),
    ("knowledge", "知识库（RAG）", "/knowledge", "ReadOutlined", "data_mgmt"),
    ("sys_mgmt", "系统管理", "", "SettingOutlined", None),
    ("role", "角色管理", "/roles", "TeamOutlined", "sys_mgmt"),
    ("group", "用户组管理", "/groups", "UsergroupAddOutlined", "sys_mgmt"),
    ("user", "用户管理", "/users", "UserOutlined", "sys_mgmt"),
    ("permission", "权限控制", "/permissions", "SafetyOutlined", "sys_mgmt"),
    ("model", "模型配置", "/models", "RobotOutlined", "sys_mgmt"),
    ("audit", "审计日志", "/audit", "FileSearchOutlined", "sys_mgmt"),
    ("saved", "结果复用 / 定时任务", "/saved", "ScheduleOutlined", None),
]


def seed_default_menus(db: Session):
    """初始化功能菜单树；admin 角色默认拥有全部菜单。
    查询豁免软删过滤：菜单/角色若被逻辑删除则复用并恢复（避免唯一约束冲突）。"""
    from ..models import RoleMenu
    code_to_id: dict[str, int] = {}
    for code, name, path, icon, parent in MENU_SEED:
        m = (db.query(Menu).execution_options(include_deleted=True)
             .filter(Menu.code == code).first())
        pid = code_to_id.get(parent, 0) if parent else 0
        if not m:
            m = Menu(code=code, name=name, path=path, icon=icon,
                     parent_id=pid, sort_order=len(code_to_id))
            db.add(m)
            db.flush()
        else:
            m.name, m.path, m.icon, m.parent_id = name, path, icon, pid
            m.is_deleted = False
        code_to_id[code] = m.id
    db.commit()
    admin = (db.query(Role).execution_options(include_deleted=True)
             .filter(Role.code == "admin").first())
    if admin:
        admin.is_deleted = False
        all_ids = [m.id for m in db.query(Menu).all()]
        have = {rm.menu_id for rm in (db.query(RoleMenu)
                                      .execution_options(include_deleted=True)
                                      .filter(RoleMenu.role_id == admin.id).all())}
        for mid in all_ids:
            if mid not in have:
                db.add(RoleMenu(role_id=admin.id, menu_id=mid))
        db.commit()
