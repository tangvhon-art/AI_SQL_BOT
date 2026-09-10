"""QuerySpec：AI 问数结构化查询参数（L2 意图理解层的唯一产物）。

QuerySpec 是全链路唯一事实来源：
QuerySpec → 数据源三层映射 → 分析规则 → SQL → 结果 → 四层结论。
本模块同时提供时间表达解析（确定性规则）与意图词典加载。
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

# ---------- 意图常量 ----------
INTENTS = ("value", "compare", "ranking", "trend", "detail", "statistic")
INTENT_LABELS = {
    "value": "数值查询", "compare": "对比分析", "ranking": "排序榜单",
    "trend": "趋势分析", "detail": "筛选明细", "statistic": "统计分析",
}
INTENT_ACTIONS = ("sum", "count", "distinct_count", "avg", "max", "min", "ratio")

# 默认口径：未指定时间时的查询窗口
DEFAULT_TIME_DAYS = 30


# ---------- 结构化参数模型 ----------
class MetricSpec(BaseModel):
    name: str = ""                 # 自然语言指标名（用户说的）
    alias: str = ""                # 映射后的字段中文注释/别名
    agg: str = "sum"               # sum/count/distinct_count/avg/max/min
    unit: str = ""                 # 单位（万元/元/单/人）
    source: str = "user"           # user/llm/dict


class BucketSpec(BaseModel):
    """分箱维度（范围分布）：如「文件大小范围」= 对 size 字段按区间分组（直方图）。

    - field：分箱目标字段名（SPEC 阶段可为空，mapping 阶段解析为真实列）
    - ranges/labels：区间边界与标签一一对应，如 [[0,1048576],...] ↔ ["0-1MB",...]
    - left_closed：左闭右开（默认），生成 CASE WHEN v>=a AND v<b
    """
    field: str = ""
    ranges: list[list[float]] = Field(default_factory=list)  # [[min, max], ...]
    labels: list[str] = Field(default_factory=list)          # 与 ranges 一一对应
    unit: str = ""                                            # KB/MB/元/天
    left_closed: bool = True


class DimensionSpec(BaseModel):
    name: str = ""
    alias: str = ""
    granularity: str | None = None  # day/week/month/quarter/year
    bucket: BucketSpec | None = None  # 范围分布/分箱维度
    source: str = "user"


class FilterSpec(BaseModel):
    field: str = ""                # 自然语言条件字段
    op: str = "="                  # =/!=/>/>=/</<=/like/in
    value: Any = None
    source: str = "user"


class TimeSpec(BaseModel):
    type: str = "relative"         # relative | range
    expr: str = ""                 # 原始表达（如 本月/近7天）
    start: str = ""                # 解析后的绝对区间 YYYY-MM-DD
    end: str = ""
    granularity: str | None = None  # day/week/month/quarter/year


class ActionSpec(BaseModel):
    top_n: int | None = None
    sort: str | None = None        # desc/asc
    compare_target: str | None = None  # mom 环比 / yoy 同比 / dim 维度对比
    percent_of: str | None = None  # 占比基数
    stat: str | None = None        # sum/avg/max/min/ratio/distinct
    detail_columns: list[str] = []  # 明细查询指定列


class QuerySpec(BaseModel):
    intent: str = "value"
    schema_name: str = ""            # 查询范围 schema（项目/库）；空 = 数据源默认
    metrics: list[MetricSpec] = Field(default_factory=list)
    dimensions: list[DimensionSpec] = Field(default_factory=list)
    filters: list[FilterSpec] = Field(default_factory=list)
    time: TimeSpec = Field(default_factory=TimeSpec)
    action: ActionSpec = Field(default_factory=ActionSpec)
    confidence: float = 0.5
    missing: list[str] = Field(default_factory=list)  # 缺失要素（触发澄清依据）
    original_question: str = ""       # 用户原始问题（溯源）
    rewritten_question: str = ""      # 问题重构后的规范化描述（溯源，空=未重构）
    table_hints: list[str] = Field(default_factory=list)  # 拆表检索词（问题重构提取，供选表阶段表检索）
    inherited_from: dict[str, Any] = Field(default_factory=dict)  # 多轮继承标注


def spec_to_dict(spec: QuerySpec) -> dict:
    d = spec.model_dump(mode="json")
    # 内部标记（__count__:X）仅用于模板/LLM 路由，对外展示为干净指标名
    for m in d.get("metrics", []):
        if (m.get("name") or "").startswith("__count__:"):
            m["name"] = m["name"][len("__count__:"):]
    return d


def spec_from_dict(data: dict | None) -> QuerySpec | None:
    if not data:
        return None
    try:
        return QuerySpec.model_validate(data)
    except Exception:  # noqa: BLE001
        return None


# ---------- 时间表达解析（确定性规则） ----------
_DATE_RE = re.compile(r"^(\d{4})[-年](\d{1,2})月?$")
_DAY_RE = re.compile(r"^(\d{4})[-年](\d{1,2})[-月](\d{1,2})日?$")
_RANGE_RE = re.compile(
    r"^(\d{4})[-年](\d{1,2})[-月](\d{1,2})日?\s*[~～至到-]\s*(\d{4})[-年](\d{1,2})[-月](\d{1,2})日?$")
_RECENT_RE = re.compile(r"(?:近|最近|过去|前)(\d{1,3})\s*(天|日|周|个?月)")
_NEXT_PREV_MONTH_RE = re.compile(r"(上|本|这|当|今|下)\s*(个?月|月)")
_NEXT_PREV_QUARTER_RE = re.compile(r"(上|本|这|当|今|下)\s*(个?季度|季度|季)")
_NEXT_PREV_YEAR_RE = re.compile(r"(去|今|本|这|明)\s*年")


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


def _fmt(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def parse_time_expr(expr: str, now: datetime | None = None) -> TimeSpec | None:
    """把自然语言时间表达解析为绝对区间；无法解析返回 None。"""
    if not expr or not expr.strip():
        return None
    now = now or datetime.now()
    today = now.date()
    e = expr.strip()

    # 1) 绝对日期范围：2026-08-01 ~ 2026-08-31
    m = _RANGE_RE.match(e)
    if m:
        s = date(int(m[1]), int(m[2]), int(m[3]))
        en = date(int(m[4]), int(m[5]), int(m[6]))
        return TimeSpec(type="range", expr=e, start=_fmt(s), end=_fmt(en),
                        granularity=auto_granularity(s, en))

    # 2) 绝对日期：2026年8月 / 2026-08-05
    m = _DAY_RE.match(e)
    if m:
        d = date(int(m[1]), int(m[2]), int(m[3]))
        return TimeSpec(type="range", expr=e, start=_fmt(d), end=_fmt(d), granularity="day")
    m = _DATE_RE.match(e)
    if m:
        s, en = _month_range(int(m[1]), int(m[2]))
        return TimeSpec(type="range", expr=e, start=_fmt(s), end=_fmt(en),
                        granularity=auto_granularity(s, en))

    # 3) 近N天/近N周/近N个月
    m = _RECENT_RE.search(e)
    if m:
        n = int(m[1])
        unit = m[2]
        if unit in ("天", "日"):
            s, en = today - timedelta(days=n - 1), today
        elif unit == "周":
            s, en = today - timedelta(days=7 * n - 1), today
        else:  # 个月
            y, mo = today.year, today.month
            for _ in range(n):
                mo -= 1
                if mo == 0:
                    mo, y = 12, y - 1
            s, _ = _month_range(y, mo)
            en = today
        return TimeSpec(type="range", expr=e, start=_fmt(s), end=_fmt(en),
                        granularity=auto_granularity(s, en))

    # 4) 今日/今日/昨天/前天
    if re.fullmatch(r"(今天|今日|当天|本日)", e):
        return TimeSpec(type="range", expr=e, start=_fmt(today), end=_fmt(today), granularity="day")
    if re.fullmatch(r"(昨天|昨日)", e):
        d = today - timedelta(days=1)
        return TimeSpec(type="range", expr=e, start=_fmt(d), end=_fmt(d), granularity="day")
    if re.fullmatch(r"(前天|前日)", e):
        d = today - timedelta(days=2)
        return TimeSpec(type="range", expr=e, start=_fmt(d), end=_fmt(d), granularity="day")

    # 5) 本周/上周/本月/上月/本季度/上季度/今年/去年
    m = re.fullmatch(r"(本|这|当)?周", e)
    if m:
        s = today - timedelta(days=today.weekday())
        return TimeSpec(type="range", expr=e, start=_fmt(s), end=_fmt(today), granularity="day")
    if re.fullmatch(r"上周", e):
        monday = today - timedelta(days=today.weekday() + 7)
        s, en = monday, monday + timedelta(days=6)
        return TimeSpec(type="range", expr=e, start=_fmt(s), end=_fmt(en), granularity="day")

    m = _NEXT_PREV_MONTH_RE.search(e)
    if m and ("月" in m.group(0)):
        label = m.group(1)
        if label in ("本", "这", "当", "今"):
            s, en = _month_range(today.year, today.month)
            return TimeSpec(type="range", expr=e, start=_fmt(s), end=_fmt(en),
                            granularity=auto_granularity(s, en))
        if label == "上":
            y, mo = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
            s, en = _month_range(y, mo)
            return TimeSpec(type="range", expr=e, start=_fmt(s), end=_fmt(en),
                            granularity=auto_granularity(s, en))
        if label == "下":
            y, mo = (today.year, today.month + 1) if today.month < 12 else (today.year + 1, 1)
            s, en = _month_range(y, mo)
            return TimeSpec(type="range", expr=e, start=_fmt(s), end=_fmt(en),
                            granularity=auto_granularity(s, en))

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
        return TimeSpec(type="range", expr=e, start=_fmt(s), end=_fmt(en),
                        granularity=auto_granularity(s, en))

    m = _NEXT_PREV_YEAR_RE.search(e)
    if m:
        label = m.group(1)
        y = today.year + (1 if label == "明" else -1 if label == "去" else 0)
        return TimeSpec(type="range", expr=e, start=_fmt(date(y, 1, 1)), end=_fmt(date(y, 12, 31)),
                        granularity="month")

    # 6) 上个月/去年同月/去年同期等同比表达：返回标记，由对比逻辑解析
    if "同期" in e or "同比" in e:
        return TimeSpec(type="relative", expr=e, granularity="month")

    return None


def shift_period(start: str, end: str, kind: str = "mom", granularity: str = "month") -> tuple[str, str]:
    """计算对比期（环比/同比）的绝对区间：返回 (prev_start, prev_end)。"""
    s = date.fromisoformat(start)
    en = date.fromisoformat(end)
    if kind == "yoy":
        s2 = date(s.year - 1, s.month, s.day)
        e2 = date(en.year - 1, en.month, en.day)
        return _fmt(s2), _fmt(e2)
    # 环比：按粒度向前推一个周期
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
    # month
    y, mo = s.year, s.month
    y2, mo2 = (y, mo - 1) if mo > 1 else (y - 1, 12)
    s2, _ = _month_range(y2, mo2)
    _, e2 = _month_range(y, mo)
    return _fmt(s2), _fmt(e2)


_SCHEMA_PROJECT_RE = re.compile(r"([A-Za-z0-9_]{1,24})(?:项目|库|数据库)")


def extract_schema_expr(question: str) -> str:
    """从问题中提取项目/库名（schema 候选），如「RT项目」→ RT；无则返回空串。

    规则：英文/数字/下划线标识 + 「项目|库|数据库」后缀；
    仅拉丁字符名可提取（schema_name 约定为英文字段），中文库名（如"元数据库"）不误提取。
    """
    if not question:
        return ""
    m = _SCHEMA_PROJECT_RE.search(question)
    return m.group(1).strip() if m else ""


def extract_time_expr(question: str) -> str:
    """从问题中抽取时间表达片段（用于规则解析）；无则返回空串。"""
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
            # "去年同期/同期" 是同比标记而非独立时间范围：交由对比逻辑处理
            if "同期" in frag:
                return ""
            return frag
    return ""


# ---------- 意图词典加载 ----------
def load_intent_dicts(db, workspace_id: int) -> dict[str, list[dict]]:
    """加载工作空间意图词典：{dict_type: [{"term","aliases","target_table","target_column","agg","unit"}]}。"""
    from ..models import IntentDict
    out: dict[str, list[dict]] = {t: [] for t in ("metric", "dimension", "time", "filter", "action")}
    rows = (db.query(IntentDict)
            .filter(IntentDict.workspace_id == workspace_id, IntentDict.enabled.is_(True))
            .all())
    for r in rows:
        if r.dict_type not in out:
            continue
        out[r.dict_type].append({
            "term": r.term, "aliases": r.aliases or [],
            "target_table": r.target_table or "", "target_column": r.target_column or "",
            "agg": r.agg or "", "unit": r.unit or "",
        })
    return out


def load_intent_templates(db, workspace_id: int) -> list[dict]:
    """加载句式模板：按优先级降序。"""
    from ..models import IntentTemplate
    rows = (db.query(IntentTemplate)
            .filter(IntentTemplate.workspace_id == workspace_id,
                    IntentTemplate.enabled.is_(True))
            .order_by(IntentTemplate.priority.desc()).all())
    return [{"pattern": r.pattern, "intent": r.intent,
             "slot_map": r.slot_map or {}} for r in rows]


# ---------- 多查询 Spec（C11 扩展） ----------
class SubQuerySpec(BaseModel):
    """单个子查询的结构化参数，字段与 QuerySpec 对齐，可通过 to_query_spec() 转换。"""
    sub_id: str = ""                       # 子查询唯一标识 q1/q2/...
    question: str = ""                     # 拆解后的自然语言
    intent: str = "value"                  # value/compare/ranking/trend/detail/statistic
    metrics: list[MetricSpec] = Field(default_factory=list)
    dimensions: list[DimensionSpec] = Field(default_factory=list)
    filters: list[FilterSpec] = Field(default_factory=list)
    time: TimeSpec = Field(default_factory=TimeSpec)
    action: ActionSpec = Field(default_factory=ActionSpec)
    schema_name: str = ""
    table_hints: list[str] = Field(default_factory=list)
    chart_hint: str | None = None          # 推荐图表类型（推荐器填充）
    title: str | None = None               # 卡片标题（LLM 生成）
    confidence: float = 0.5                # 要素提取置信度（低置信度前端高亮提示）
    enabled: bool = True                   # 用户确认时是否启用（停用项不执行）
    # 选表澄清（Phase A 探测）：子查询选表歧义时，预览面板展示候选表供用户确认
    needs_tables: bool = False             # 需要用户确认查询表
    candidate_tables: list[dict] = Field(default_factory=list)   # [{table, comment}]
    confirmed_tables: list[str] = Field(default_factory=list)    # 用户澄清确认的表（confirm 携带）

    def to_query_spec(self) -> QuerySpec:
        """转换为旧版 QuerySpec，传入 generate_sql_stream。"""
        return QuerySpec(
            intent=self.intent,
            schema_name=self.schema_name,
            metrics=self.metrics,
            dimensions=self.dimensions,
            filters=self.filters,
            time=self.time,
            action=self.action,
            original_question=self.question,
            rewritten_question=self.question,
            table_hints=self.table_hints,
        )

    def to_dict(self) -> dict:
        """JSON 序列化（落库 / 单卡重试定位 / dashboard 组装用）。"""
        return self.model_dump(mode="json")


class MultiQuerySpec(BaseModel):
    """多查询拆解结果：原始问题 + 子查询列表 + 布局提示。"""
    original_question: str = ""
    sub_queries: list[SubQuerySpec] = Field(default_factory=list)
    layout_hint: str = "auto"             # auto/grid_2/grid_3/tabs/masonry
    shared_dimension: str | None = None    # 共享维度（联动用）
    source: str = "rule"                   # rule/llm/manual
    task_id: str = ""                      # 两阶段编排任务标识（multi_spec 下发，confirm 幂等用）

    def is_single(self) -> bool:
        """是否单查询（长度=1 时走旧链路兼容）。"""
        return len(self.sub_queries) <= 1

    def enabled_queries(self) -> list[SubQuerySpec]:
        """用户确认后启用的子查询列表。"""
        return [s for s in self.sub_queries if s.enabled]

    def to_dict(self) -> dict:
        return self.model_dump(mode="json")
