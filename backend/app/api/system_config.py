"""系统配置 API：读取生效配置（.env 默认 + DB 覆盖）与保存平台级覆盖。

GET  /system-config          → {sections: {cache: {...}}, overridden: [字段名]}
PUT  /system-config          → body {sections: {...}}，upsert 覆盖并即时生效
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config_override import current_overrides, get_effective, set_overrides
from ..database import get_db
from ..models import SystemConfig
from .deps import get_current_user

router = APIRouter(prefix="/system-config", tags=["system-config"])

# 配置分组 → 字段名列表（用于前端分组展示；字段以 config.Settings 为准）
SECTIONS: dict[str, list[str]] = {
    "cache": ["cache_enabled", "cache_ttl_result_sec", "cache_ttl_gen_sec",
              "cache_similarity", "cache_max_entries_per_ws"],
    "cost_guard": ["cost_guard_enabled", "cost_guard_explain_timeout_ms",
                   "cost_guard_mysql_rows_soft", "cost_guard_mysql_rows_hard",
                   "cost_guard_pg_cost_soft", "cost_guard_pg_cost_hard"],
    "rate_limit": ["rate_limit_enabled", "rate_limit_qps", "rate_limit_burst",
                   "rate_limit_max_concurrent", "rate_limit_queue_timeout_s",
                   "query_timeout_ms"],
    "schema_sync": ["schema_sync_collect_samples", "schema_sync_sample_per_column",
                    "schema_sync_sample_min_rows", "schema_sync_sample_timeout_s"],
    "lineage": ["lineage_collect_enabled", "lineage_ddl_interval_min",
                "lineage_mine_interval_min", "lineage_max_depth",
                "lineage_parse_timeout_s"],
    "eval": ["eval_mock_execute"],
}


class SystemConfigIn(BaseModel):
    sections: dict[str, dict]  # {section: {key: value}}


def _build_sections() -> tuple[dict, list[str]]:
    eff = get_effective()
    data = eff.model_dump()
    overridden = set(current_overrides().keys())
    sections: dict[str, dict] = {}
    for section, keys in SECTIONS.items():
        sections[section] = {}
        for k in keys:
            if k in data:
                sections[section][k] = data[k]
    return sections, sorted(overridden)


@router.get("")
def get_system_config(db: Session = Depends(get_db), user=Depends(get_current_user)):
    sections, overridden = _build_sections()
    return {"sections": sections, "overridden": overridden}


@router.put("")
def put_system_config(body: SystemConfigIn, db: Session = Depends(get_db),
                      user=Depends(get_current_user)):
    """保存平台级配置覆盖（全量替换：提交内容即新覆盖集，缺省字段恢复默认），写入后即时生效。"""
    allowed = {k for keys in SECTIONS.values() for k in keys}
    flat: dict = {}
    for section, kv in body.sections.items():
        if section not in SECTIONS:
            continue
        for k, v in kv.items():
            if k not in allowed or v is None:
                continue
            flat[k] = v

    # 全量替换：删除全部覆盖后按提交内容重插（清空字段 = 恢复默认）
    db.query(SystemConfig).delete()
    for k, v in flat.items():
        section = next((s for s, keys in SECTIONS.items() if k in keys), "")
        db.add(SystemConfig(section=section, key=k, value_json=v))
    db.commit()
    set_overrides(flat)
    sections, overridden = _build_sections()
    return {"sections": sections, "overridden": overridden}
