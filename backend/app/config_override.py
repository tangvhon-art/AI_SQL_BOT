"""系统配置覆盖层：.env/环境变量为默认，DB 覆盖（system_config 表）优先，内存即时生效。

用法：
- 启动时 load_overrides_from_db(db) 载入已保存覆盖
- 引擎/服务读取配置统一改用 get_effective()（替代 get_settings()），覆盖即时生效
- PUT /api/v1/system-config 时调用 set_overrides(flat) 更新内存 + 持久化
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# 平台级覆盖：{config字段名: value}（字段名与 config.Settings 一致）
_OVERRIDES: dict[str, Any] = {}


def load_overrides_from_db(db) -> int:
    """启动时加载已持久化的系统配置覆盖；返回加载条数。失败静默（不影响启动）。"""
    global _OVERRIDES
    try:
        from .models import SystemConfig
        rows = db.query(SystemConfig).all()
        _OVERRIDES = {r.key: r.value_json for r in rows}
        if rows:
            logger.info("[sys-config] 已加载 %d 条系统配置覆盖", len(rows))
        return len(rows)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[sys-config] 加载覆盖失败: %s", exc)
        return 0


def set_overrides(flat: dict[str, Any]) -> None:
    """更新内存覆盖（调用方负责持久化）。"""
    _OVERRIDES.clear()
    _OVERRIDES.update({k: v for k, v in flat.items() if v is not None})


def current_overrides() -> dict[str, Any]:
    return dict(_OVERRIDES)


def get_effective():
    """返回合并覆盖后的 Settings 实例（默认值被 DB 覆盖替换）。"""
    from .config import Settings, get_settings
    if not _OVERRIDES:
        return get_settings()
    data = get_settings().model_dump()
    for k, v in _OVERRIDES.items():
        if k in data and v is not None:
            data[k] = v
    return Settings(**data)
