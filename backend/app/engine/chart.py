"""图表引擎：AI 推荐图表类型 → 规则兜底 → 标准 ECharts option。

L-A: LLM 根据意图 + 数据形态推荐图表类型
L-C: 规则兜底（数据形态推断），使用公共模块判断类型/语义
"""
from __future__ import annotations

import logging
from typing import Any

from .schema_types import is_time_field, looks_date, is_numeric as _is_num_type
from .biz_lexicon import match as lex_match
from .llm_json import ask as llm_json_ask

logger = logging.getLogger(__name__)

_VALID_CHART_TYPES = frozenset({
    "metric", "line", "area", "bar", "stacked_bar", "grouped_bar", "pie", "table",
})


def _to_number(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        v = v.replace(",", "")
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _is_numeric(v: Any) -> bool:
    return _to_number(v) is not None


def _numeric_ratio(rows: list[list[Any]], col_idx: int) -> float:
    if not rows:
        return 0.0
    return sum(1 for r in rows if col_idx < len(r) and _is_numeric(r[col_idx])) / len(rows)


# ────────────── L-A: LLM 图表类型推荐 ──────────────

def _llm_recommend_chart_type(columns: list[str], rows: list[list[Any]],
                              intent: str | None, llm: Any) -> str | None:
    """LLM 根据意图 + 列名 + 数据预览推荐图表类型。

    返回有效 chart_type 或 None（降级到规则兜底）。
    """
    if llm is None or not getattr(llm, "configured", False):
        return None

    preview_rows = rows[:5]
    preview = "\n".join(
        " | ".join(str(c) for c in r) for r in preview_rows
    ) if preview_rows else "（无数据）"

    system = (
        "你是数据可视化专家。根据用户查询意图、列名和数据预览，推荐最合适的图表类型。\n"
        "可选类型：metric（单一数值指标）、line（折线趋势）、area（面积趋势）、"
        "bar（柱状对比）、stacked_bar（堆叠柱状）、grouped_bar（分组柱状）、"
        "pie（饼图占比）、table（明细表格）。\n"
        "规则：单行单列→metric；时间序列→line/area；占比构成→pie；分类对比→bar；"
        "多指标对比→grouped_bar；明细列表→table。\n"
        '严格输出 JSON：{"chart_type": "类型名", "reason": "简短理由"}'
    )
    user = (
        f"查询意图：{intent or '未指定'}\n"
        f"列名：{', '.join(columns)}\n"
        f"数据预览（前{len(preview_rows)}行）：\n{preview}\n"
        f"总行数：{len(rows)}"
    )

    data = llm_json_ask(llm, system, user, max_tokens=300, temperature=0.0)
    if not data:
        return None
    ct = (data.get("chart_type") or "").strip().lower()
    if ct in _VALID_CHART_TYPES:
        logger.debug("[chart] LLM 推荐: %s (%s)", ct, data.get("reason", ""))
        return ct
    logger.warning("[chart] LLM 返回无效类型 %r, 降级规则兜底", ct)
    return None


# ────────────── L-C: 规则兜底（rule_fallback） ──────────────

def _rule_fallback_chart_type(columns: list[str], rows: list[list[Any]],
                              intent: str | None = None) -> str:
    """规则兜底：数据形态推断图表类型（无 LLM 或 LLM 失败时使用）。

    使用公共模块 schema_types / biz_lexicon 替代散落的类型判断。
    """
    if not rows or len(rows) == 0:
        return "table"
    n_cols = len(columns)
    n_rows = len(rows)
    if n_cols == 1:
        return "metric" if n_rows == 1 else "table"

    first = rows[0][0]
    has_date = is_time_field(columns[0], "", "") or looks_date(first)

    metric_cols = [i for i in range(1, n_cols) if _numeric_ratio(rows, i) >= 0.5]
    has_numeric = len(metric_cols) > 0

    # 饼图：2 列（维度+数值），且指标列含占比语义 或 数值总和接近 100
    if n_cols == 2 and has_numeric:
        if lex_match("ratio", columns[1]):
            return "pie"
        total = sum(r[1] for r in rows if _is_numeric(r[1]))
        if 80 <= total <= 120 and n_rows <= 15:
            return "pie"

    # 时间序列
    if has_date and has_numeric:
        if len(metric_cols) >= 2:
            return "area"
        return "line"

    # 分类对比
    if has_numeric:
        if n_cols == 2:
            return "bar"
        if len(metric_cols) >= 2 and n_rows <= 12:
            return "stacked_bar"
        return "grouped_bar"

    return "table"


def build_chart_option(columns: list[str], rows: list[list[Any]],
                       chart_type: str | None = None,
                       intent: str | None = None,
                       llm: Any = None) -> dict:
    """生成 ECharts option。

    策略：chart_type 显式指定 → LLM 推荐（intent+数据形态）→ 规则兜底。
    所有分支均携带 columns/rows 供表格视图渲染。
    返回 dict 含 _source 字段标识图表类型来源（explicit / llm / rule_fallback）。
    """
    if not columns or not rows:
        return {"type": "table", "columns": columns, "rows": rows, "_source": "rule_fallback"}

    ctype = chart_type
    source = "explicit"
    if not ctype:
        # L-A: LLM 推荐
        ctype = _llm_recommend_chart_type(columns, rows, intent, llm)
        if ctype:
            source = "llm"
    if not ctype:
        # L-C: 规则兜底
        ctype = _rule_fallback_chart_type(columns, rows, intent)
        source = "rule_fallback"

    if ctype == "metric":
        return {
            "type": "metric",
            "value": rows[0][0],
            "label": columns[0],
            "columns": columns,
            "rows": rows,
            "_source": source,
        }

    if ctype == "pie":
        x_col, y_col = columns[0], columns[1]
        data = [{"name": str(r[0]), "value": _to_number(r[1]) or 0}
                for r in rows if len(r) >= 2]
        return {
            "type": "pie",
            "option": {
                "tooltip": {"trigger": "item", "formatter": "{b}: {c} ({d}%)"},
                "legend": {"type": "scroll", "orient": "vertical", "right": 10, "top": "center"},
                "series": [{
                    "name": y_col,
                    "type": "pie",
                    "radius": ["40%", "70%"],
                    "center": ["40%", "50%"],
                    "avoidLabelOverlap": True,
                    "itemStyle": {"borderRadius": 6, "borderColor": "#fff", "borderWidth": 2},
                    "label": {"show": True, "formatter": "{b}\n{d}%"},
                    "data": data,
                }],
            },
            "columns": columns,
            "rows": rows,
            "_source": source,
        }

    if ctype in ("line", "bar", "grouped_bar", "stacked_bar", "area"):
        x_col, y_cols = columns[0], columns[1:]
        x_data = [str(r[0]) for r in rows]
        series = []
        is_stack = ctype == "stacked_bar"
        is_area = ctype == "area"
        base_type = "line" if ctype in ("line", "area") else "bar"
        for i, yc in enumerate(y_cols):
            s = {
                "name": yc,
                "type": base_type,
                "data": [_to_number(r[i + 1]) or 0 for r in rows],
            }
            if is_stack:
                s["stack"] = "total"
                s["emphasis"] = {"focus": "series"}
            if is_area:
                s["areaStyle"] = {"opacity": 0.3}
                s["smooth"] = True
            series.append(s)
        return {
            "type": ctype,
            "option": {
                "tooltip": {"trigger": "axis"},
                "legend": {"data": y_cols},
                "grid": {"left": 56, "right": 24, "top": 44, "bottom": 56},
                "xAxis": {"type": "category", "data": x_data, "name": x_col,
                          "axisLabel": {"interval": 0, "rotate": 30 if len(x_data) > 8 else 0}},
                "yAxis": {"type": "value"},
                "series": series,
            },
            "columns": columns,
            "rows": rows,
            "_source": source,
        }

    return {"type": "table", "columns": columns, "rows": rows, "_source": source}
