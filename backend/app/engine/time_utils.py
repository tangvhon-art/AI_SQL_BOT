"""L-0 公共化地基层：时间表达解析 / 时间片段提取 / 粒度推断 / 日期格式检测。

重构自 query_spec.py 的 parse_time_expr / auto_granularity / extract_time_expr /
extract_schema_expr / shift_period，将散落于 query_spec / intent / chart / mapping
的时间逻辑统一收拢。
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

DEFAULT_TIME_DAYS = 30

_DATE_RE = re.compile(r"^(\d{4})[-年](\d{1,2})月?$")
_DAY_RE = re.compile(r"^(\d{4})[-年](\d{1,2})[-月](\d{1,2})日?$")
_RANGE_RE = re.compile(
    r"^(\d{4})[-年](\d{1,2})[-月](\d{1,2})日?\s*[~～至到-]\s*"
    r"(\d{4})[-年](\d{1,2})[-月](\d{1,2})日?$")
_RECENT_RE = re.compile(r"(?:近|最近|过去|前)(\d{1,3})\s*(天|日|周|个?月)")
_NEXT_PREV_MONTH_RE = re.compile(r"(上|本|这|当|今|下)\s*(个?月|月)")
_NEXT_PREV_QUARTER_RE = re.compile(r"(上|本|这|当|今|下)\s*(个?季度|季度|季)")
_NEXT_PREV_YEAR_RE = re.compile(r"(去|今|本|这|明)\s*年")
_SCHEMA_PROJECT_RE = re.compile(r"([A-Za-z0-9_]{1,24})(?:项目|库|数据库)")


def _fmt(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def _month_range(y: int, m: int) -> tuple[date, date]:
    if m == 12:
        end = date(y + 1, 1, 1) - timedelta(days=1)
    else:
        end = date(y, m + 1, 1) - timedelta(days=1)
    return date(y, m, 1), end


def _quarter_range(y: int, q: int) -> tuple[date, date]:
    start = date(y, (q - 1) * 3 + 1, 1)
    end = _month_range(y, (q - 1) * 3 + 3)[1]
    return start, end


def auto_granularity(start: date, end: date) -> str:
    """按时间跨度自动定粒度：≤45 天按日；≤400 天按月；否则按季度。"""
    days = (end - start).days
    if days <= 45:
        return "day"
    if days <= 400:
        return "month"
    return "quarter"


def parse_time_expr(expr: str, now: datetime | None = None) -> dict | None:
    """把自然语言时间表达解析为绝对区间；无法解析返回 None。

    返回 dict: {type, expr, start, end, granularity}。
    """
    if not expr or not expr.strip():
        return None
    now = now or datetime.now()
    today = now.date()
    e = expr.strip()

    m = _RANGE_RE.match(e)
    if m:
        s = date(int(m[1]), int(m[2]), int(m[3]))
        en = date(int(m[4]), int(m[5]), int(m[6]))
        return {"type": "range", "expr": e, "start": _fmt(s), "end": _fmt(en),
                "granularity": auto_granularity(s, en)}

    m = _DAY_RE.match(e)
    if m:
        d = date(int(m[1]), int(m[2]), int(m[3]))
        return {"type": "range", "expr": e, "start": _fmt(d), "end": _fmt(d),
                "granularity": "day"}

    m = _DATE_RE.match(e)
    if m:
        s, en = _month_range(int(m[1]), int(m[2]))
        return {"type": "range", "expr": e, "start": _fmt(s), "end": _fmt(en),
                "granularity": auto_granularity(s, en)}

    m = _RECENT_RE.search(e)
    if m:
        n = int(m[1])
        unit = m[2]
        if unit in ("天", "日"):
            s, en = today - timedelta(days=n - 1), today
        elif unit == "周":
            s, en = today - timedelta(days=7 * n - 1), today
        else:
            y, mo = today.year, today.month
            for _ in range(n):
                mo -= 1
                if mo == 0:
                    mo, y = 12, y - 1
            s, _ = _month_range(y, mo)
            en = today
        return {"type": "range", "expr": e, "start": _fmt(s), "end": _fmt(en),
                "granularity": auto_granularity(s, en)}

    if re.fullmatch(r"(今天|今日|当天|本日)", e):
        return {"type": "range", "expr": e, "start": _fmt(today), "end": _fmt(today),
                "granularity": "day"}
    if re.fullmatch(r"(昨天|昨日)", e):
        d = today - timedelta(days=1)
        return {"type": "range", "expr": e, "start": _fmt(d), "end": _fmt(d),
                "granularity": "day"}
    if re.fullmatch(r"(前天|前日)", e):
        d = today - timedelta(days=2)
        return {"type": "range", "expr": e, "start": _fmt(d), "end": _fmt(d),
                "granularity": "day"}

    m = re.fullmatch(r"(本|这|当)?周", e)
    if m:
        s = today - timedelta(days=today.weekday())
        return {"type": "range", "expr": e, "start": _fmt(s), "end": _fmt(today),
                "granularity": "day"}
    if re.fullmatch(r"上周", e):
        monday = today - timedelta(days=today.weekday() + 7)
        s, en = monday, monday + timedelta(days=6)
        return {"type": "range", "expr": e, "start": _fmt(s), "end": _fmt(en),
                "granularity": "day"}

    m = _NEXT_PREV_MONTH_RE.search(e)
    if m and "月" in m.group(0):
        label = m.group(1)
        if label in ("本", "这", "当", "今"):
            s, en = _month_range(today.year, today.month)
            return {"type": "range", "expr": e, "start": _fmt(s), "end": _fmt(en),
                    "granularity": auto_granularity(s, en)}
        if label == "上":
            y, mo = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
            s, en = _month_range(y, mo)
            return {"type": "range", "expr": e, "start": _fmt(s), "end": _fmt(en),
                    "granularity": auto_granularity(s, en)}
        if label == "下":
            y, mo = (today.year, today.month + 1) if today.month < 12 else (today.year + 1, 1)
            s, en = _month_range(y, mo)
            return {"type": "range", "expr": e, "start": _fmt(s), "end": _fmt(en),
                    "granularity": auto_granularity(s, en)}

    m = _NEXT_PREV_QUARTER_RE.search(e)
    if m:
        q = (today.month - 1) // 3 + 1
        label = m.group(1)
        if label in ("本", "这", "当", "今"):
            s, en = _quarter_range(today.year, q)
        elif label == "上":
            y, qq = (today.year, q - 1) if q > 1 else (today.year - 1, 4)
            s, en = _quarter_range(y, qq)
        else:
            y, qq = (today.year, q + 1) if q < 4 else (today.year + 1, 1)
            s, en = _quarter_range(y, qq)
        return {"type": "range", "expr": e, "start": _fmt(s), "end": _fmt(en),
                "granularity": auto_granularity(s, en)}

    m = _NEXT_PREV_YEAR_RE.search(e)
    if m:
        label = m.group(1)
        y = today.year + (1 if label == "明" else -1 if label == "去" else 0)
        return {"type": "range", "expr": e,
                "start": _fmt(date(y, 1, 1)), "end": _fmt(date(y, 12, 31)),
                "granularity": "month"}

    if "同期" in e or "同比" in e:
        return {"type": "relative", "expr": e, "granularity": "month"}

    return None


def shift_period(start: str, end: str, kind: str = "mom",
                 granularity: str = "month") -> tuple[str, str]:
    """计算对比期（环比/同比）的绝对区间：返回 (prev_start, prev_end)。"""
    s = date.fromisoformat(start)
    en = date.fromisoformat(end)
    if kind == "yoy":
        s2 = date(s.year - 1, s.month, s.day)
        e2 = date(en.year - 1, en.month, en.day)
        return _fmt(s2), _fmt(e2)
    g = granularity or auto_granularity(s, en)
    if g == "day":
        span = (en - s).days + 1
        return _fmt(s - timedelta(days=span)), _fmt(s - timedelta(days=1))
    if g == "week":
        span = (en - s).days + 1
        return _fmt(s - timedelta(days=span)), _fmt(s - timedelta(days=1))
    if g == "quarter":
        q = (s.month - 1) // 3 + 1
        s2, _ = _quarter_range(s.year - 1 if q == 1 else s.year, 4 if q == 1 else q - 1)
        _, e2 = _quarter_range(s.year, q)
        return _fmt(s2), _fmt(e2)
    y, mo = s.year, s.month
    y2, mo2 = (y, mo - 1) if mo > 1 else (y - 1, 12)
    s2, _ = _month_range(y2, mo2)
    _, e2 = _month_range(y, mo)
    return _fmt(s2), _fmt(e2)


def extract_time_expr(question: str) -> str:
    """从问题中抽取时间表达片段（用于规则解析）；无则返回空串。"""
    if not question:
        return ""
    for pat in (
        r"(?:近|最近|过去|前)\d{1,3}\s*(?:天|日|周|个?月)",
        r"\d{4}[-年]\d{1,2}[-月]\d{1,2}日?\s*[~～至到-]\s*\d{4}[-年]\d{1,2}[-月]\d{1,2}日?",
        r"\d{4}[-年]\d{1,2}月?",
        r"\d{4}[-年]\d{1,2}[-月]\d{1,2}日?",
        r"(?:今天|今日|当天|本日|昨天|昨日|前天|前日)",
        r"(?:本|这|当|今|上|下)\s*(?:个?月|月)",
        r"(?:本|这|当|今|上|下)\s*(?:个?季度|季度|季)",
        r"(?:上周|本周)",
        r"(?:去|今|本|这|明)\s*年",
    ):
        m = re.search(pat, question)
        if m:
            frag = m.group(0).strip()
            if "同期" in frag:
                return ""
            return frag
    return ""


def extract_schema_expr(question: str) -> str:
    """从问题中提取项目/库名（schema 候选），如「RT项目」→ RT；无则返回空串。"""
    if not question:
        return ""
    m = _SCHEMA_PROJECT_RE.search(question)
    return m.group(1).strip() if m else ""


def default_time_range() -> dict:
    """返回默认时间窗口（近 30 天）。"""
    today = datetime.now().date()
    return {
        "type": "range",
        "expr": "近30天（默认）",
        "start": (today - timedelta(days=29)).strftime("%Y-%m-%d"),
        "end": today.strftime("%Y-%m-%d"),
        "granularity": "day",
    }
