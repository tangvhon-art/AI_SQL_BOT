"""图表引擎：结果特征分析 → 类型推荐 → 标准 ECharts option。
支持 metric / line / area / bar / stacked_bar / grouped_bar / pie / table。"""
from typing import Any


def _to_number(v: Any) -> float | None:
    """将 int/float/Decimal/数字字符串转为 float；bool、非数字字符串、None 返回 None。"""
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


def _looks_date(v: Any) -> bool:
    s = str(v)
    if len(s) >= 8 and s[:4].isdigit() and s[4:6].isdigit():
        return True
    return "-" in s and s.split("-")[0].isdigit()


def _date_hints(col: str) -> bool:
    c = col.lower()
    return any(k in c for k in ("date", "time", "day", "month", "year")) or \
        any(k in col for k in ("日期", "时间", "月", "年"))


def _ratio_hints(col: str) -> bool:
    c = col.lower()
    return any(k in c for k in ("rate", "ratio", "percent", "pct", "proportion", "share")) or \
        any(k in col for k in ("占比", "比例", "百分比", "构成"))


def _guess_chart_type(columns: list[str], rows: list[list[Any]]) -> str:
    if not rows or len(rows) == 0:
        return "table"
    n_cols = len(columns)
    n_rows = len(rows)
    if n_cols == 1:
        return "metric" if n_rows == 1 else "table"

    first = rows[0][0]
    has_date = _date_hints(columns[0]) or _looks_date(first)

    # 数值指标列（第 2 列起）
    metric_cols = [i for i in range(1, n_cols) if _numeric_ratio(rows, i) >= 0.5]
    has_numeric = len(metric_cols) > 0

    # 饼图：2 列（维度+数值），且指标列含占比语义 或 数值总和接近 100（强信号）
    if n_cols == 2 and has_numeric:
        if _ratio_hints(columns[1]):
            return "pie"
        total = sum(r[1] for r in rows if _is_numeric(r[1]))
        if 80 <= total <= 120 and n_rows <= 15:
            return "pie"

    # 时间序列
    if has_date and has_numeric:
        # 多指标时间趋势 → 面积图（更直观展示累积/趋势）
        if len(metric_cols) >= 2:
            return "area"
        return "line"

    # 分类对比
    if has_numeric:
        if n_cols == 2:
            return "bar"
        # 多指标分组：指标列≥2 且维度基数少 → 堆叠柱状图（展示构成）
        if len(metric_cols) >= 2 and n_rows <= 12:
            return "stacked_bar"
        return "grouped_bar"

    return "table"


# 意图 → 图表类型映射（意图优先，形状兜底）
_INTENT_CHART: dict[str, str] = {
    "value": "metric",
    "trend": "line",
    "ranking": "bar",
    "compare": "bar",
    "statistic": "pie",
    "detail": "table",
}


def _intent_chart_type(intent: str | None, columns: list[str],
                       rows: list[list[Any]]) -> str | None:
    """按意图选图表：数据形态不支持时返回 None（交给形状兜底）。"""
    if not intent or intent not in _INTENT_CHART:
        return None
    want = _INTENT_CHART[intent]
    n = len(rows) if rows else 0
    nc = len(columns)
    if want == "metric":
        return "metric" if n == 1 and nc == 1 else None
    if want == "pie":
        # 占比：维度+数值 或 数据含 占比 列；多指标列（≥2数值列）走分组柱状图而非饼图
        if nc >= 2 and n >= 2:
            metric_cols = [i for i in range(1, nc) if _numeric_ratio(rows, i) >= 0.5]
            if len(metric_cols) >= 2:
                return "grouped_bar"
            if _ratio_hints(columns[-1]):
                return "pie"
            return "bar"
        return None
    if want == "line":
        if n >= 2 and nc >= 2:
            return "area" if nc >= 3 else "line"
        return None
    if want == "bar":
        if n >= 1 and nc >= 2:
            return "stacked_bar" if (nc >= 3 and n <= 12) else ("grouped_bar" if nc >= 3 else "bar")
        return None
    if want == "table":
        return "table"
    return None


def build_chart_option(columns: list[str], rows: list[list[Any]],
                       chart_type: str | None = None,
                       intent: str | None = None) -> dict:
    """生成 ECharts option（数据行列已由后端限制 ≤1000）。所有分支均携带 columns/rows 供表格视图渲染。
    策略：意图优先（intent 参数）→ 形状兜底（chart_type=None 时自动推断）。"""
    if not columns or not rows:
        return {"type": "table", "columns": columns, "rows": rows}
    ctype = chart_type
    if not ctype:
        ctype = _intent_chart_type(intent, columns, rows)
    if not ctype:
        ctype = _guess_chart_type(columns, rows)

    if ctype == "metric":
        return {
            "type": "metric",
            "value": rows[0][0],
            "label": columns[0],
            "columns": columns,
            "rows": rows,
        }

    if ctype == "pie":
        # 饼图：第一列维度名，第二列数值
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
        }

    return {"type": "table", "columns": columns, "rows": rows}
