"""公共 Prompt 服务（多场景：AI解读/洞察草案/SQL生成等）。

统一管理 Prompt 模板的加载、变量渲染、默认模板选择。
各场景通过 scene_type 隔离，支持 workspace 级自定义 + 全局内置。
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from ..models import Prompt


class PromptService:
    """公共 Prompt 服务。

    用法：
        svc = PromptService(db, workspace_id=1)
        template = svc.get_default("ai_interpret")
        rendered = svc.render(template.prompt_template, {"question": "...", "data_summary": "..."})
    """

    def __init__(self, db: Session, workspace_id: int = 0):
        self.db = db
        self.workspace_id = workspace_id

    def list(self, scene_type: str | None = None, keyword: str = "") -> list[Prompt]:
        """列出模板：按 scene_type 过滤 + 关键词搜索。"""
        q = self.db.query(Prompt).filter(
            Prompt.workspace_id.in_([0, self.workspace_id]),
            Prompt.is_deleted.is_(False),
        )
        if scene_type:
            q = q.filter(Prompt.scene_type == scene_type)
        if keyword:
            q = q.filter(Prompt.name.contains(keyword))
        return q.order_by(Prompt.is_builtin.desc(), Prompt.sort_order.asc(), Prompt.id.asc()).all()

    def get(self, prompt_id: int) -> Prompt | None:
        return self.db.query(Prompt).filter(Prompt.id == prompt_id, Prompt.is_deleted.is_(False)).first()

    def get_default(self, scene_type: str) -> Prompt | None:
        """获取场景默认模板：优先 workspace 级默认，其次全局内置默认。"""
        # workspace 级默认
        ws_default = self.db.query(Prompt).filter(
            Prompt.workspace_id == self.workspace_id,
            Prompt.scene_type == scene_type,
            Prompt.is_default.is_(True),
            Prompt.is_deleted.is_(False),
        ).first()
        if ws_default:
            return ws_default
        # 全局内置默认
        return self.db.query(Prompt).filter(
            Prompt.workspace_id == 0,
            Prompt.scene_type == scene_type,
            Prompt.is_default.is_(True),
            Prompt.is_builtin.is_(True),
            Prompt.is_deleted.is_(False),
        ).first()

    def create(self, scene_type: str, name: str, prompt_template: str,
               description: str = "", scene_tags: str = "",
               is_default: bool = False, created_by: int = 0) -> Prompt:
        """创建自定义模板。"""
        if is_default:
            # 取消同场景其他默认
            self.db.query(Prompt).filter(
                Prompt.workspace_id == self.workspace_id,
                Prompt.scene_type == scene_type,
                Prompt.is_default.is_(True),
            ).update({Prompt.is_default: False})
        p = Prompt(
            workspace_id=self.workspace_id,
            scene_type=scene_type,
            name=name,
            description=description,
            scene_tags=scene_tags,
            prompt_template=prompt_template,
            is_default=is_default,
            is_builtin=False,
            created_by=created_by,
        )
        self.db.add(p)
        self.db.commit()
        self.db.refresh(p)
        return p

    def update(self, prompt_id: int, **fields) -> Prompt | None:
        """更新模板（内置模板同样允许编辑）。"""
        p = self.get(prompt_id)
        if not p:
            return None
        if fields.get("is_default") and not p.is_default:
            self.db.query(Prompt).filter(
                Prompt.workspace_id == self.workspace_id,
                Prompt.scene_type == p.scene_type,
                Prompt.is_default.is_(True),
            ).update({Prompt.is_default: False})
        for k, v in fields.items():
            if hasattr(p, k) and v is not None:
                setattr(p, k, v)
        self.db.commit()
        self.db.refresh(p)
        return p

    def delete(self, prompt_id: int) -> bool:
        """删除模板（软删除，内置模板同样允许删除）。"""
        p = self.get(prompt_id)
        if not p:
            return False
        p.is_deleted = True
        self.db.commit()
        return True

    def set_default(self, prompt_id: int) -> bool:
        """设为场景默认。"""
        p = self.get(prompt_id)
        if not p:
            return False
        self.db.query(Prompt).filter(
            Prompt.workspace_id == self.workspace_id,
            Prompt.scene_type == p.scene_type,
            Prompt.is_default.is_(True),
        ).update({Prompt.is_default: False})
        p.is_default = True
        self.db.commit()
        return True

    @staticmethod
    def render(template: str, variables: dict[str, Any]) -> str:
        """渲染 Prompt 模板：替换 {{变量名}} 占位符。

        未提供的变量保留原样（不报错），便于 LLM 自行处理。
        """
        def replace(match: re.Match) -> str:
            key = match.group(1).strip()
            return str(variables.get(key, match.group(0)))
        return re.sub(r"\{\{\s*(\w+)\s*\}\}", replace, template)

    @staticmethod
    def extract_variables(template: str) -> list[str]:
        """提取模板中的变量名列表。"""
        return list(set(re.findall(r"\{\{\s*(\w+)\s*\}\}", template)))
