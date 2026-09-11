"""
菜单同步脚本：将前端菜单配置同步到数据库 menu 表
使用方式：cd backend && .venv/bin/python sync_menus.py

新增菜单后运行此脚本，会自动插入缺失的菜单（按 code 去重），不会修改已有菜单。
"""
from app.database import SessionLocal
from app.models import Menu

# 菜单定义（与前端 src/config/menu.tsx 保持一致）
# 格式：(code, name, path, icon, parent_code, sort_order)
MENUS = [
    # 一级菜单
    ('chat', 'AI 问数', '/chat', 'comment', None, 10),
    ('data_mgmt', '数据管理', '', 'database', None, 20),
    ('sys_mgmt', '系统管理', '', 'setting', None, 30),
    ('cap_mgmt', '能力增强', '', 'experiment', None, 40),
    # 数据管理子菜单
    ('datasource', '数据源管理', '/datasources', 'api', 'data_mgmt', 10),
    ('knowledge', '知识库（RAG）', '/knowledge', 'filetext', 'data_mgmt', 20),
    # 系统管理子菜单
    ('role', '角色管理', '/roles', 'team', 'sys_mgmt', 10),
    ('group', '用户组管理', '/groups', 'usergroup', 'sys_mgmt', 20),
    ('user', '用户管理', '/users', 'user', 'sys_mgmt', 30),
    ('permission', '权限控制', '/permissions', 'safety', 'sys_mgmt', 40),
    ('model', '模型配置', '/models', 'robot', 'sys_mgmt', 50),
    ('prompts', 'Prompt管理', '/prompts', 'filetext', 'sys_mgmt', 60),
    ('audit', '审计日志', '/audit', 'audit', 'sys_mgmt', 70),
    ('menu_config', '菜单中心', '/menu-config', 'menu', 'sys_mgmt', 80),
    # 能力增强子菜单
    ('eval', '评测中心', '/eval', 'experiment', 'cap_mgmt', 10),
    ('scenes', '场景模板', '/scenes', 'appstore', 'cap_mgmt', 20),
    ('cache', '缓存管理', '/cache', 'thunderbolt', 'cap_mgmt', 30),
    ('sys_config', '系统配置', '/sys-config', 'control', 'cap_mgmt', 40),
    ('reports', '报告中心', '/reports', 'filetext', 'cap_mgmt', 50),
    ('insight', '洞察分析', '/insight', 'thunderbolt', 'cap_mgmt', 60),
]


def main():
    db = SessionLocal()
    try:
        # 建立 code -> id 映射
        code_to_id = {}
        for m in db.query(Menu).all():
            code_to_id[m.code] = m.id

        added = 0
        skipped = 0
        for code, name, path, icon, parent_code, sort in MENUS:
            if code in code_to_id:
                skipped += 1
                continue
            parent_id = code_to_id.get(parent_code, 0) if parent_code else 0
            m = Menu(
                parent_id=parent_id,
                code=code,
                name=name,
                path=path,
                icon=icon,
                sort_order=sort,
            )
            db.add(m)
            db.flush()
            code_to_id[code] = m.id
            added += 1
            print(f'  + {name} ({code}) parent={parent_code or "root"}')

        db.commit()
        total = db.query(Menu).count()
        print(f'\n同步完成：新增 {added} 个，已存在 {skipped} 个，当前共 {total} 个菜单')
    finally:
        db.close()


if __name__ == '__main__':
    main()
