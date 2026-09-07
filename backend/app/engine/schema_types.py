"""L-0 公共化地基层：字段类型分类 / ID·系统字段识别 / 时间字段识别。

合并 mapping._NUMERIC/_DATE/_STRING_TYPES、chart._is_numeric/_date_hints、
nl2sql.SYSTEM_TABLE_BLACKLIST、intent ID 过滤等散落于 4+ 文件的类型判断逻辑。
"""
from __future__ import annotations

import re
from typing import Any

NUMERIC_TYPES = frozenset({
    "int", "integer", "bigint", "smallint", "tinyint", "decimal",
    "numeric", "float", "double", "number",
})

DATE_TYPES = frozenset({
    "date", "datetime", "timestamp", "time", "year",
})

STRING_TYPES = frozenset({
    "varchar", "char", "text", "longtext", "mediumtext", "tinytext",
    "string", "enum", "set",
})

_SYSTEM_FIELD_NAMES = frozenset({
    "id", "is_deleted", "create_user", "update_user",
    "create_time", "update_time", "delete_time",
})

SYSTEM_TABLES = frozenset({
    "column_meta", "table_meta", "datasource", "permission_rule", "users", "user", "role",
    "conversation", "conversation_message", "query_log", "saved_query",
    "faq_pair", "knowledge_doc", "sql_example", "relationship", "user_group",
    "workspace", "audit_log", "model_config", "menu", "user_group_role",
    "doc_chunk", "document_chunk", "kb_doc", "knowledge_chunk",
    "scheduled_task", "task_run_log", "sys_celery_task_log", "sys_crontab",
})

_TIME_KEYWORDS = ("日期", "时间", "date", "time", "day", "month", "year", "月", "年", "日")


def _normalize_type(data_type: str) -> str:
    return (data_type or "").lower().split("(")[0].strip()


def is_numeric(data_type: str) -> bool:
    """数值类型判断。合并 mapping._is_numeric_type / chart._is_numeric。"""
    return _normalize_type(data_type) in NUMERIC_TYPES


def is_date(data_type: str) -> bool:
    """日期类型判断。合并 mapping._is_date_type。"""
    return _normalize_type(data_type) in DATE_TYPES


def is_string(data_type: str) -> bool:
    """字符串类型判断。合并 mapping._is_string_type。"""
    return _normalize_type(data_type) in STRING_TYPES


def is_time_field(col_name: str, comment: str, data_type: str = "") -> bool:
    """时间字段综合判断：类型判断 + 统一时间关键词表。

    合并 mapping._time_like / chart._date_hints。
    col_name/comment/data_type 从 ColumnMeta 传入。
    """
    if is_date(data_type):
        return True
    c = f"{comment or ''} {col_name or ''}".lower()
    return any(k in c for k in _TIME_KEYWORDS)


def is_system_field(col_name: str) -> bool:
    """系统/审计字段识别（id / is_deleted / create_user / update_user / create_time / update_time / delete_time）。

    合并 mapping 排除列表 / intent ID 过滤。
    """
    cname = (col_name or "").lower().strip()
    return cname in _SYSTEM_FIELD_NAMES or cname.endswith("_id")


def is_id_field(col_name: str) -> bool:
    """ID/外键类字段判断（id 或 *_id 结尾）。"""
    cname = (col_name or "").lower().strip()
    return cname == "id" or cname.endswith("_id")


def looks_date(value: Any) -> bool:
    """判断值是否看起来像日期（合并 chart._looks_date）。"""
    s = str(value or "")
    if len(s) >= 8 and s[:4].isdigit() and s[4:6].isdigit():
        return True
    return "-" in s and s.split("-")[0].isdigit()
