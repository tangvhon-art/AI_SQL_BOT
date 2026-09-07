"""L3 分析规则引擎：6 大意图标准分析规则（确定性计算）+ SQL 模板生成 + 异常检测。

设计原则：能规则计算的（求和/占比/同比环比/TopN/异常检测）一律代码实现，
LLM 只负责"理解"和"解读"，不负责"计算"——保证同类问题结果一致、可复算、可审计。

SQL 生成：标准场景直接由模板生成（确定性）；模板无法覆盖（多表/复杂映射）时
返回 use_llm=True，由上层走 NL2SQL LLM 路径（现有校验链保留）。
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Any

from .query_spec import QuerySpec, shift_period

logger = logging.getLogger(__name__)


class AnalysisError(Exception):
    """模板无法覆盖 → 上层降级 LLM 路径。"""


# ---------- SQL 片段工具 ----------
def _quote_value(v: Any, op: str = "=") -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v).replace("'", "''")
    if op.lower() == "like":
        return f"'%{s}%'"
    return f"'{s}'"


def _where_sql(mapping: dict, spec: QuerySpec, time_range: tuple[str, str] | None,
               dialect: str = "mysql") -> str:
    conds: list[str] = []
    tcol = (mapping.get("time_field") or {}).get("column") or ""
    for f in mapping.get("filters", []):
        col = f["column"]
        table = f.get("table") or ""
        pref = _qtable(table, spec, dialect) if table else ""
        conds.append(f"{pref + '.' if pref else ''}`{col}` {f['op']} {_quote_value(f['value'], f['op'])}")
    if time_range and tcol:
        # 相对时间（近N天/昨天/本月等）：参数化为基于 CURDATE() 的区间，不写死固定日期
        rel_cond = _relative_time_cond(tcol, spec.time.expr) if spec.time else None
        if rel_cond:
            conds.append(rel_cond)
        else:
            s, e = time_range
            conds.append(f"`{tcol}` BETWEEN '{s}' AND '{e}'")
    return (" WHERE " + " AND ".join(conds)) if conds else ""


_REL_DAY_RE = re.compile(r"^(?:近|最近|过去|前)\s*(\d{1,3})\s*(?:天|日)")
_REL_WEEK_RE = re.compile(r"^(?:近|最近|过去|前)\s*(\d{1,3})\s*周")
_REL_MONTH_RE = re.compile(r"^(?:近|最近|过去|前)\s*(\d{1,3})\s*个?月")


def _relative_time_cond(tcol: str, expr: str) -> str | None:
    """相对时间表达 → 基于数据库当前日期的参数化条件（左闭右开，含今天/本周/本月）。

    命中相对时间（近N天/昨天/本月/本周/今年等）返回条件 SQL；无法识别返回 None（走字面量）。
    起点取当天 0 点、终点取截止日次日 0 点；「过去N天/近N天」= 含今天在内的最近 N 个自然日。
    """
    e = (expr or "").strip()
    if not e:
        return None
    # 过去N天/近N天/最近N天/前N天（含今天；容忍「（默认）」等后缀）
    m = _REL_DAY_RE.match(e)
    if m:
        n = int(m.group(1))
        return (f"`{tcol}` >= DATE_SUB(CURDATE(), INTERVAL {n - 1} DAY) "
                f"AND `{tcol}` < DATE_ADD(CURDATE(), INTERVAL 1 DAY)")
    # 昨天 / 今天 / 前天
    if e.startswith("昨天"):
        return f"`{tcol}` >= DATE_SUB(CURDATE(), INTERVAL 1 DAY) AND `{tcol}` < CURDATE()"
    if e.startswith("今天"):
        return f"`{tcol}` >= CURDATE() AND `{tcol}` < DATE_ADD(CURDATE(), INTERVAL 1 DAY)"
    if e.startswith("前天"):
        return (f"`{tcol}` >= DATE_SUB(CURDATE(), INTERVAL 2 DAY) "
                f"AND `{tcol}` < DATE_SUB(CURDATE(), INTERVAL 1 DAY)")
    # 近N周（含本周）
    m = _REL_WEEK_RE.match(e)
    if m:
        n = int(m.group(1))
        return (f"`{tcol}` >= DATE_SUB(CURDATE(), INTERVAL {n * 7 - 1} DAY) "
                f"AND `{tcol}` < DATE_ADD(CURDATE(), INTERVAL 1 DAY)")
    # 近N个月（含本月）
    m = _REL_MONTH_RE.match(e)
    if m:
        n = int(m.group(1))
        return (f"`{tcol}` >= DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL {n - 1} MONTH), '%Y-%m-01') "
                f"AND `{tcol}` < DATE_ADD(DATE_FORMAT(CURDATE(), '%Y-%m-01'), INTERVAL 1 MONTH)")
    # 本月 / 上月 / 本周 / 上周 / 今年 / 去年
    if e.startswith("本月") or e.startswith("这月") or e.startswith("当月") or e.startswith("这个月"):
        return (f"`{tcol}` >= DATE_FORMAT(CURDATE(), '%Y-%m-01') "
                f"AND `{tcol}` < DATE_ADD(DATE_FORMAT(CURDATE(), '%Y-%m-01'), INTERVAL 1 MONTH)")
    if e.startswith("上月") or e.startswith("上个月"):
        return (f"`{tcol}` >= DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 1 MONTH), '%Y-%m-01') "
                f"AND `{tcol}` < DATE_FORMAT(CURDATE(), '%Y-%m-01')")
    if e.startswith("本周") or e.startswith("这周"):
        return (f"`{tcol}` >= DATE_SUB(CURDATE(), INTERVAL WEEKDAY(CURDATE()) DAY) "
                f"AND `{tcol}` < DATE_ADD(DATE_SUB(CURDATE(), INTERVAL WEEKDAY(CURDATE()) DAY), INTERVAL 1 WEEK)")
    if e.startswith("上周"):
        return (f"`{tcol}` >= DATE_SUB(DATE_SUB(CURDATE(), INTERVAL WEEKDAY(CURDATE()) DAY), INTERVAL 1 WEEK) "
                f"AND `{tcol}` < DATE_SUB(CURDATE(), INTERVAL WEEKDAY(CURDATE()) DAY)")
    if e.startswith("今年") or e.startswith("本年"):
        return (f"`{tcol}` >= DATE_FORMAT(CURDATE(), '%Y-01-01') "
                f"AND `{tcol}` < DATE_ADD(DATE_FORMAT(CURDATE(), '%Y-01-01'), INTERVAL 1 YEAR)")
    if e.startswith("去年") or e.startswith("上年"):
        return (f"`{tcol}` >= DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 1 YEAR), '%Y-01-01') "
                f"AND `{tcol}` < DATE_FORMAT(CURDATE(), '%Y-01-01')")
    return None


def _time_filter(mapping: dict, spec: QuerySpec) -> tuple[str, str] | None:
    if spec.time and spec.time.start and spec.time.end:
        return spec.time.start, spec.time.end
    return None


def _trend_expr(tcol: str, granularity: str, dialect: str = "mysql") -> str:
    g = granularity or "day"
    if g == "month":
        return f"DATE_FORMAT(`{tcol}`, '%Y-%m')"
    if g == "quarter":
        return f"CONCAT(YEAR(`{tcol}`), '-Q', QUARTER(`{tcol}`))"
    if g == "week":
        return f"DATE_FORMAT(`{tcol}`, '%x-W%v')"
    return f"DATE_FORMAT(`{tcol}`, '%Y-%m-%d')"


def _main_table(mapping: dict) -> str:
    tables = {m["table"] for m in mapping.get("metrics", [])}
    tables |= {d["table"] for d in mapping.get("dimensions", [])}
    tables |= {f["table"] for f in mapping.get("filters", [])}
    tables.discard("")
    if len(tables) > 1:
        raise AnalysisError("涉及多张数据表，需走 LLM 关联路径")
    if not tables:
        raise AnalysisError("未解析出数据表")
    return tables.pop()


def _clean_label(comment: str) -> str:
    """标签/别名清洗：只取主名，去掉注释中的枚举/取值说明（项目ID（NULL=全局…）→项目ID）。"""
    for sep in ("（", "(", "：", ":", "·", "-", " "):
        idx = comment.find(sep)
        if idx > 0:
            comment = comment[:idx]
    return comment.strip()


def _qtable(t: str, spec: QuerySpec, dialect: str = "mysql") -> str:
    """表名限定：指定 schema（项目/库）且为 mysql 方言时输出 `schema`.`table`；
    否则输出裸表名（连接库即该 schema）。"""
    if spec.schema and dialect == "mysql":
        return f"`{spec.schema}`.`{t}`"
    return f"`{t}`"


# ---------- 六大意图 SQL 模板 ----------
def _sql_value(spec: QuerySpec, mapping: dict, dialect: str) -> str:
    t = _qtable(_main_table(mapping), spec, dialect)
    m = mapping["metrics"][0]
    tr = _time_filter(mapping, spec)
    agg = m["agg"]
    if agg == "count":
        expr = "COUNT(*)"
    elif agg == "distinct_count":
        expr = f"COUNT(DISTINCT `{m['column']}`)"
    else:
        expr = f"{agg.upper()}(`{m['column']}`)"
    label = _clean_label(m["comment"] or m["column"])
    return f"SELECT {expr} AS `{label}` FROM {t}{_where_sql(mapping, spec, tr, dialect)}"


def _sql_ranking(spec: QuerySpec, mapping: dict, dialect: str) -> str:
    t = _qtable(_main_table(mapping), spec, dialect)
    if not mapping["dimensions"]:
        raise AnalysisError("榜单查询缺少分组维度")
    d = mapping["dimensions"][0]
    m = mapping["metrics"][0]
    agg = m["agg"]
    expr = f"COUNT(*)" if agg == "count" else f"{agg.upper()}(`{m['column']}`)"
    n = spec.action.top_n or 10
    sort = "ASC" if spec.action.sort == "asc" else "DESC"
    tr = _time_filter(mapping, spec)
    dlabel = _clean_label(d["comment"] or d["column"])
    mlabel = _clean_label(m["comment"] or m["column"])
    return (f"SELECT `{d['column']}` AS `{dlabel}`, {expr} AS `{mlabel}` "
            f"FROM {t}{_where_sql(mapping, spec, tr, dialect)} "
            f"GROUP BY `{d['column']}` ORDER BY 2 {sort} LIMIT {n}")


def _sql_trend(spec: QuerySpec, mapping: dict, dialect: str) -> str:
    t = _qtable(_main_table(mapping), spec, dialect)
    tcol = (mapping.get("time_field") or {}).get("column") or ""
    if not tcol:
        raise AnalysisError("趋势分析缺少时间字段")
    m = mapping["metrics"][0]
    agg = m["agg"]
    expr = f"COUNT(*)" if agg == "count" else f"{agg.upper()}(`{m['column']}`)"
    g = spec.time.granularity or "day"
    te = _trend_expr(tcol, g)
    tr = _time_filter(mapping, spec)
    mlabel = _clean_label(m["comment"] or m["column"])
    return (f"SELECT {te} AS `日期`, {expr} AS `{mlabel}` FROM {t}"
            f"{_where_sql(mapping, spec, tr, dialect)} GROUP BY 1 ORDER BY 1 ASC")


def _sql_statistic(spec: QuerySpec, mapping: dict, dialect: str) -> str:
    t = _qtable(_main_table(mapping), spec, dialect)
    m = mapping["metrics"][0]
    agg = m["agg"]
    stat = spec.action.stat
    tr = _time_filter(mapping, spec)
    if stat == "ratio" and mapping["dimensions"]:
        d = mapping["dimensions"][0]
        expr = f"SUM(`{m['column']}`)" if agg == "sum" else f"{agg.upper()}(`{m['column']}`)"
        dlabel = _clean_label(d["comment"] or d["column"])
        mlabel = _clean_label(m["comment"] or m["column"])
        sub_where = _where_sql(mapping, spec, tr, dialect)
        return (f"SELECT `{d['column']}` AS `{dlabel}`, {expr} AS `{mlabel}`, "
                f"ROUND({expr} * 100.0 / (SELECT {expr} FROM {t}{sub_where}), 2) AS `占比%` "
                f"FROM {t}{sub_where} GROUP BY `{d['column']}` ORDER BY 2 DESC")
    # 其他统计：sum/avg/max/min/distinct → 单值；若同时带维度（各X/每个X/按X统计）→ 按维度分组
    if agg == "count":
        expr = "COUNT(*)"
    elif agg == "distinct_count":
        expr = f"COUNT(DISTINCT `{m['column']}`)"
    else:
        agg_use = {"avg": "AVG", "max": "MAX", "min": "MIN", "sum": "SUM"}.get(stat or agg, "SUM")
        expr = f"{agg_use}(`{m['column']}`)"
    label = _clean_label(m["comment"] or m["column"])
    if mapping["dimensions"]:
        d = mapping["dimensions"][0]
        dtable = (d.get("table") or "").strip()
        tname = t.strip("`").split(".")[-1] if "`" in t else t
        if dtable and dtable != tname:
            raise AnalysisError("统计维度跨表（维度在 %s，指标在 %s），需走 LLM 关联路径"
                                % (dtable, tname))
        dlabel = _clean_label(d["comment"] or d["column"])
        return (f"SELECT `{d['column']}` AS `{dlabel}`, {expr} AS `{label}` "
                f"FROM {t}{_where_sql(mapping, spec, tr, dialect)} "
                f"GROUP BY `{d['column']}` ORDER BY 2 DESC")
    return f"SELECT {expr} AS `{label}` FROM {t}{_where_sql(mapping, spec, tr, dialect)}"


def _sql_detail(spec: QuerySpec, mapping: dict, dialect: str) -> str:
    t = _qtable(_main_table(mapping), spec, dialect)
    cols = []
    tcol = (mapping.get("time_field") or {}).get("column") or ""
    if tcol:
        cols.append(f"`{tcol}`")
    for m in mapping["metrics"][:5]:
        cols.append(f"`{m['column']}` AS `{m['comment'] or m['column']}`")
    for d in mapping["dimensions"][:3]:
        cols.append(f"`{d['column']}` AS `{d['comment'] or d['column']}`")
    if not cols:
        raise AnalysisError("明细查询缺少可展示字段，需 LLM 字段检索")
    tr = _time_filter(mapping, spec)
    limit = 100
    return f"SELECT {', '.join(cols)} FROM {t}{_where_sql(mapping, spec, tr, dialect)} LIMIT {limit}"


def _sql_compare(spec: QuerySpec, mapping: dict, dialect: str) -> str:
    """对比分析：环比/同比 → 本期 vs 上期 UNION ALL；维度对比 → GROUP BY 维度。"""
    t = _qtable(_main_table(mapping), spec, dialect)
    m = mapping["metrics"][0]
    agg = m["agg"]
    expr = f"COUNT(*)" if agg == "count" else f"{agg.upper()}(`{m['column']}`)"
    mlabel = _clean_label(m["comment"] or m["column"])
    target = spec.action.compare_target or "mom"
    tr = _time_filter(mapping, spec)
    if target == "dim":
        if not mapping["dimensions"]:
            raise AnalysisError("维度对比缺少分组维度")
        d = mapping["dimensions"][0]
        dlabel = _clean_label(d["comment"] or d["column"])
        return (f"SELECT `{d['column']}` AS `{dlabel}`, {expr} AS `{mlabel}` "
                f"FROM {t}{_where_sql(mapping, spec, tr, dialect)} GROUP BY `{d['column']}` ORDER BY 2 DESC")
    # 环比/同比：本期 vs 上期
    if not tr:
        raise AnalysisError("对比分析缺少时间范围")
    cur = _where_sql(mapping, spec, tr, dialect)
    prev_s, prev_e = shift_period(tr[0], tr[1], kind=target,
                                  granularity=spec.time.granularity or "month")
    prev_where = _where_sql(mapping, spec, (prev_s, prev_e), dialect)
    cur_sql = (f"SELECT '本期' AS `比较期`, {expr} AS `{mlabel}` "
               f"FROM {t}{cur}")
    prev_sql = (f"SELECT '上期' AS `比较期`, {expr} AS `{mlabel}` "
                f"FROM {t}{prev_where}")
    return f"{cur_sql} UNION ALL {prev_sql}"


def build_analysis_sql(spec: QuerySpec, mapping: dict,
                       dialect: str = "mysql") -> dict:
    """按 6 大意图生成确定性 SQL。

    返回 {"sql": str, "analysis_meta": {...}}
    模板无法覆盖时抛 AnalysisError → 上层走 LLM 兜底路径。
    """
    intent = spec.intent
    # 计数类指标（调用次数/访问次数/请求次数等）：COUNT 的统计表必须按问题语义确定
    # （如"接口调用次数"应统计执行记录表 api_executions，而非接口定义表），模板无法覆盖，
    # 强制走 LLM 兜底（防止模板生成"每个接口恒为1条"的错误结果）
    if any(m.agg == "count" and (any(k in (m.name or "") for k in ("次数", "调用", "访问", "请求"))
                                 or (m.name or "").startswith("__count__:"))
           for m in spec.metrics):
        raise AnalysisError("计数类指标（次数/调用/访问/请求）需 LLM 按问题确定统计来源表")
    if intent == "value":
        sql = _sql_value(spec, mapping, dialect)
    elif intent == "ranking":
        sql = _sql_ranking(spec, mapping, dialect)
    elif intent == "trend":
        sql = _sql_trend(spec, mapping, dialect)
    elif intent == "statistic":
        sql = _sql_statistic(spec, mapping, dialect)
    elif intent == "detail":
        sql = _sql_detail(spec, mapping, dialect)
    elif intent == "compare":
        sql = _sql_compare(spec, mapping, dialect)
    else:
        raise AnalysisError(f"未知意图: {intent}")
    meta = {
        "intent": intent,
        "schema": spec.schema,
        "metric": mapping["metrics"][0] if mapping["metrics"] else None,
        "dimensions": [_clean_label(d["comment"] or d["column"]) for d in mapping["dimensions"]],
        "time": spec_to_time_meta(spec),
        "compare_target": spec.action.compare_target,
        "top_n": spec.action.top_n,
        "stat": spec.action.stat,
    }
    return {"sql": sql, "analysis_meta": meta}


def spec_to_time_meta(spec: QuerySpec) -> dict:
    return {"expr": spec.time.expr, "start": spec.time.start,
            "end": spec.time.end, "granularity": spec.time.granularity}


# ---------- 结果事实计算（确定性，供四层结论使用） ----------
def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    # 字符串先去千分位逗号；Decimal / numpy 数值 / 数字字符串等统一尝试 float 转换
    if isinstance(v, str):
        v = v.replace(",", "")
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def compute_compare_facts(columns: list[str], rows: list[list]) -> dict | None:
    """从 UNION ALL 结果（本期/上期）计算差值、变化率。"""
    if len(rows) != 2:
        return None
    idx = next((i for i, c in enumerate(columns) if "比较期" not in c), None)
    if idx is None:
        return None
    cur = _num(rows[0][idx]) if len(rows[0]) > idx else None
    prev = _num(rows[1][idx]) if len(rows[1]) > idx else None
    if cur is None:
        return None
    if prev is None or prev == 0:
        return {"current": cur, "previous": prev, "diff": None, "rate": None}
    diff = round(cur - prev, 4)
    rate = round(diff / prev * 100, 2)
    return {"current": cur, "previous": prev, "diff": diff, "rate": rate}


def compute_facts(spec: QuerySpec, columns: list[str], rows: list[list],
                  compare: dict | None = None) -> dict:
    """按意图计算确定性事实摘要（四层结论的数字来源，LLM 只能引用这些数字）。"""
    intent = spec.intent
    facts: dict[str, Any] = {"intent": intent, "row_count": len(rows)}
    vals: list[float] = []
    for i, c in enumerate(columns):
        for r in rows:
            v = _num(r[i]) if i < len(r) else None
            if v is not None:
                vals.append(v)
    if intent == "value" and rows:
        # LLM 分组 SQL 可能返回"维度列+数值列"（如 接口名称, 调用次数），
        # current 取第一个可转数值的列，避免把分组字符串当指标误判为空
        cur = None
        for cell in (rows[0] or []):
            v = _num(cell)
            if v is not None:
                cur = v
                break
        facts["current"] = cur
        if cur is None:
            facts["empty"] = True
    elif intent == "compare":
        if compare:
            facts.update(compare)
        if facts.get("current") is None:
            facts["empty"] = True
    elif intent == "ranking" and rows:
        facts["top_name"] = str(rows[0][0]) if rows[0] else None
        facts["top_value"] = _num(rows[0][1]) if len(rows[0]) > 1 else None
        facts["top_n"] = len(rows)
        if facts["top_value"] is None:
            facts["empty"] = True
    elif intent == "trend":
        if not rows:
            facts["empty"] = True
        else:
            first = _num(rows[0][1]) if len(rows[0]) > 1 else None
            last = _num(rows[-1][1]) if len(rows[-1]) > 1 else None
            if first is None and last is None:
                facts["empty"] = True
            if first is not None and last is not None and first != 0:
                facts["change"] = round((last - first) / first * 100, 2)
            facts["first"] = first
            facts["last"] = last
            if vals:
                facts["avg"] = round(sum(vals) / len(vals), 2)
                facts["max"] = max(vals)
                facts["min"] = min(vals)
                peak = max(range(len(rows)), key=lambda i: _num(rows[i][1]) if len(rows[i]) > 1 and _num(rows[i][1]) is not None else -1e18)
                facts["peak"] = str(rows[peak][0])
    elif intent == "statistic" and rows:
        if spec.action.stat == "ratio":
            facts["total"] = round(sum(vals), 2) if vals else None
            if rows:
                facts["top_name"] = str(rows[0][0])
                facts["top_value"] = _num(rows[0][1]) if len(rows[0]) > 1 else None
                facts["top_ratio"] = _num(rows[0][2]) if len(rows[0]) > 2 else None
        else:
            # 单值或分组统计：收集所有可转数值单元格求和作合计（分组结果 rows>1 不误判空）
            all_nums = [v for row in rows for cell in row
                        if (v := _num(cell)) is not None]
            facts["current"] = round(sum(all_nums), 2) if all_nums else None
            if not all_nums:
                facts["empty"] = True
            # 多指标分类对比（维度列+≥2数值列）：保留完整行列明细，供 LLM 分别解读各维度各指标
            numeric_col_idx = [i for i in range(1, len(columns))
                               if all(_num(r[i]) is not None for r in rows if i < len(r))]
            if len(columns) >= 3 and len(numeric_col_idx) >= 2 and len(rows) >= 2:
                facts["multi_metric"] = True
                facts["dim_col"] = columns[0]
                facts["metric_cols"] = [columns[i] for i in numeric_col_idx]
                facts["detail_rows"] = [
                    {columns[0]: str(r[0]) if r else "",
                     **{columns[i]: _num(r[i]) for i in numeric_col_idx if i < len(r)}}
                    for r in rows[:20]
                ]
                # 各指标合计与最高项
                for mi in numeric_col_idx:
                    col_vals = [_num(r[mi]) for r in rows if mi < len(r) and _num(r[mi]) is not None]
                    if col_vals:
                        facts[f"total_{columns[mi]}"] = round(sum(col_vals), 2)
                        max_idx = max(range(len(rows)), key=lambda i: _num(rows[i][mi]) or -1e18)
                        facts[f"top_{columns[mi]}"] = {"name": str(rows[max_idx][0]), "value": max(col_vals)}
    elif intent == "detail":
        facts["columns"] = columns[:6]
    if vals:
        facts["sum"] = round(sum(vals), 2)
    return facts


# ---------- 异常检测（数据合理性校验） ----------
def detect_anomalies(spec: QuerySpec, columns: list[str], rows: list[list],
                     compare: dict | None = None, threshold: float = 50.0) -> list[dict]:
    """规则化异常检测：空数据 / 负值 / 骤增骤降 / 趋势波动。结果不拦截，只标注。"""
    anomalies: list[dict] = []
    if not rows:
        anomalies.append({"type": "empty",
                          "desc": "暂无对应周期数据，可尝试扩大时间范围或调整筛选条件"})
        return anomalies

    # 负值异常
    for i, c in enumerate(columns):
        for r in rows:
            v = _num(r[i]) if i < len(r) else None
            if v is not None and v < 0:
                anomalies.append({"type": "negative",
                                  "desc": f"字段「{c}」出现负值，建议核对业务情况或数据口径"})
                break

    # 对比类：骤增骤降
    if compare and compare.get("previous"):
        rate = compare.get("rate")
        if rate is not None and abs(rate) >= threshold:
            anomalies.append({
                "type": "spike",
                "desc": f"较上期变化 {rate:+.1f}%，超过 {threshold:.0f}% 阈值，"
                        f"建议核对业务活动或数据口径"})

    # 趋势类：单期波动幅度
    if spec.intent == "trend" and len(rows) >= 3:
        col = 1
        max_rate = 0.0
        prev_v = None
        for r in rows:
            v = _num(r[col]) if len(r) > col else None
            if v is not None and prev_v is not None and prev_v != 0:
                rate = abs((v - prev_v) / prev_v * 100)
                max_rate = max(max_rate, rate)
            if v is not None:
                prev_v = v
        if max_rate >= threshold:
            anomalies.append({
                "type": "volatile",
                "desc": f"单期最大波动 {max_rate:.1f}%，超过 {threshold:.0f}% 阈值，数据波动较大"})

    return anomalies
