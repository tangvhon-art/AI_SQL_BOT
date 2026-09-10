"""子查询要素补齐（N1 节点）：为拆解出的子问题提取结构化要素。

策略（一次 LLM 批量提取 + 规则校验 + 父级上下文继承）：
1. LLM 一次调用同时提取 N 个子查询的 intent/metrics/dimensions/time/title
2. 规则校验：intent 白名单、时间表达解析、指标/维度缺失标记 low confidence
3. 失败降级：继承父 QuerySpec 要素，标注 confidence=low，保证节点永远可产出可用结构
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from .query_spec import (MetricSpec, DimensionSpec, FilterSpec, TimeSpec,
                         SubQuerySpec, parse_time_expr, extract_time_expr)

logger = logging.getLogger(__name__)

_INTENT_WHITELIST = ("value", "compare", "ranking", "trend", "detail", "statistic")
# 图表意向词 → intent（LLM 提取缺失时的规则兜底）
_INTENT_KEYWORDS = (
    ("ranking", ("排行", "排名", "top", "前", "最多", "最高", "榜单")),
    ("trend", ("趋势", "走势", "变化", "增长", "下降", "逐", "每天", "按月")),
    ("compare", ("对比", "比较", "同比", "环比", "差异")),
    ("statistic", ("占比", "比例", "分布", "统计", "构成", "平均", "合计", "汇总", "多少", "几个")),
    ("detail", ("明细", "详情", "清单", "列表", "哪些")),
)
# 规则兜底指标：命中词 → 聚合方式（LLM 提取失败时保证 N1 有可用要素）
_METRIC_RULES = (
    ("count", ("使用次数", "次数", "个数", "数量", "人数", "笔数", "件数", "多少", "几个", "总数")),
    ("avg", ("平均", "人均", "时长", "客单价")),
    ("percent", ("占比", "比例", "份额")),
    ("sum", ("金额", "销售额", "订单量", "总额", "合计", "总量", "增长率", "营收", "成本", "利润")),
)
# 规则兜底维度（分组分类口径）
_DIM_KEYWORDS = ("楼层", "区域", "地区", "部门", "门店", "城市", "省份", "品类", "类别",
                 "类型", "状态", "来源", "渠道", "季度", "月份", "年份", "日期", "仓库",
                 "客户", "产品", "品牌", "业务线", "组织", "中心", "会议室")


def _extract_time(question: str, parent_time: TimeSpec | None) -> TimeSpec | None:
    """子问题时间提取：自身表达优先，否则继承父级时间（公共条件下沉）。"""
    expr = extract_time_expr(question)
    if expr:
        ts = parse_time_expr(expr)
        if ts:
            return ts
    # 继承父级时间（子问题未显式声明时）
    if parent_time and parent_time.expr:
        return parent_time
    return None


def _fallback_title(question: str) -> str:
    """LLM 未返回标题时的规则化回退：去除查询动词和填充词，截取核心短语。"""
    t = question.strip()
    # 去除句首查询动词
    for prefix in ("查询", "统计", "查看", "展示", "获取", "列出", "计算", "分析", "求"):
        if t.startswith(prefix):
            t = t[len(prefix):]
            break
    # 去除尾部填充词
    for suffix in ("分别是", "是哪个", "是多少", "有哪些", "的记录", "情况", "数据", "信息"):
        if t.endswith(suffix):
            t = t[: -len(suffix)]
    # 去除中间冗余词
    for word in ("分别", "各个", "每个", "所有", "全部", "的"):
        t = t.replace(word, "")
    t = t.strip("，。、；： ")
    return t[:16] if t else question[:16]


def _rule_intent(question: str) -> str:
    """规则兜底意图：按关键词命中返回首个意图，未命中返回 value。"""
    for intent, words in _INTENT_KEYWORDS:
        if any(w in question for w in words):
            return intent
    return "value"


def _rule_metrics(question: str) -> list[tuple[str, str]]:
    """规则兜底指标提取：命中关键词返回 [(名称, agg)]，未命中空列表。"""
    out: list[tuple[str, str]] = []
    for agg, words in _METRIC_RULES:
        for w in words:
            if w in question:
                out.append((w, agg))
                break
    return out


def _rule_dimensions(question: str) -> list[str]:
    """规则兜底维度提取：命中维度关键词返回名称列表。"""
    return [d for d in _DIM_KEYWORDS if d in question]


def _metric_names(metrics: list[Any]) -> list[str]:
    out = []
    for m in metrics:
        name = getattr(m, "name", "") or ""
        if name and name not in out:
            out.append(name)
    return out


def _dim_names(dims: list[Any]) -> list[str]:
    out = []
    for d in dims:
        name = getattr(d, "name", "") or ""
        if name and name not in out:
            out.append(name)
    return out


def probe_table_clarify(sub: SubQuerySpec, datasource_id: int,
                        llm: Any = None) -> dict:
    """选表探测（Phase A）：预判子查询执行时是否会触发选表澄清。

    返回 {"needs": bool, "candidates": [...], "reason": str}。
    - 选表成功且无歧义 → needs=False
    - 无表/多表歧义 → needs=True + 候选表（优先 hint 命中，其次歧义候选）
    - 探测异常/LLM 不可用 → needs=False（不阻塞拆解，执行时走原兜底）
    """
    try:
        from ..engine.nl2sql import _llm_select_tables, _detect_clarify
        from .id_resolver import narrow_candidate_tables

        tables, meta = _llm_select_tables(
            datasource_id, sub.question,
            schema_name=sub.schema_name or None,
            llm=llm, table_hints=sub.table_hints or None)
        if not tables:
            # 无可用表：候选 = 四维检索命中表（执行时 LLM 未配置也会 mock/失败）
            hits = meta.get("hint_hits") or []
            if not hits:
                return {"needs": False, "candidates": [], "reason": "no_table_no_hint"}
            raw_candidates = [{"table": t, "comment": ""} for t in hits]
            candidates = narrow_candidate_tables(datasource_id, raw_candidates, question=sub.question)
            return {"needs": True, "candidates": candidates, "reason": "no_table"}
        # LLM 选中多张表：执行时大概率触发 clarify（LLM 选表不稳定），预览阶段直接要求澄清
        if len(tables) >= 2:
            raw_candidates = [{"table": t.table_name,
                               "comment": getattr(t, "comment", "") or ""} for t in tables]
            candidates = narrow_candidate_tables(datasource_id, raw_candidates, question=sub.question)
            return {"needs": True, "candidates": candidates, "reason": "multi_table"}
        clarify = _detect_clarify(sub.question, datasource_id, tables)
        if clarify:
            candidates = narrow_candidate_tables(datasource_id, clarify, question=sub.question)
            return {"needs": True, "candidates": candidates, "reason": "ambiguous"}
        return {"needs": False, "candidates": [], "reason": "ok"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[多查询][选表探测] 子查询 %s 探测失败（跳过）: %s", sub.sub_id, exc)
        return {"needs": False, "candidates": [], "reason": "probe_error"}


def enrich_sub_specs(
    question: str,
    sub_questions: list[str],
    parent_spec: Any,
    llm: Any = None,
    parent_hints: list[str] | None = None,
    schema_name: str = "",
) -> list[SubQuerySpec]:
    """为拆解出的子问题补齐结构化要素，返回 SubQuerySpec 列表。

    parent_spec：父级 QuerySpec（可空）；其 time/schema/table_hints 用于继承。
    任何失败均降级为「规则兜底 + 父级继承」，保证节点可行。
    """
    parent_time = getattr(parent_spec, "time", None) if parent_spec else None
    parent_intent = getattr(parent_spec, "intent", "value") if parent_spec else "value"
    hints = list(parent_hints or [])
    if parent_spec and getattr(parent_spec, "table_hints", None):
        for h in parent_spec.table_hints:
            if h not in hints:
                hints.append(h)
    if parent_spec and getattr(parent_spec, "schema_name", None) and not schema_name:
        schema_name = parent_spec.schema_name

    raw: list[dict] | None = None
    if llm is not None:
        raw = _llm_extract(question, sub_questions, llm)

    specs: list[SubQuerySpec] = []
    for i, sq in enumerate(sub_questions):
        sub_id = f"q{i + 1}"
        item: dict[str, Any] = {}
        confidence = 0.5
        if raw and i < len(raw) and isinstance(raw[i], dict):
            item = raw[i]
            confidence = 0.8

        intent = str(item.get("intent") or "")
        if intent not in _INTENT_WHITELIST:
            intent = _rule_intent(sq)
            confidence = min(confidence, 0.4)

        # 指标/维度（LLM 提取 → 规则兜底关键词）
        metrics_raw = item.get("metrics") or []
        dims_raw = item.get("dimensions") or []
        metrics = []
        for name in metrics_raw:
            name = str(name).strip()
            if name and name not in _metric_names(metrics):
                metrics.append(MetricSpec(name=name, agg="sum", source="llm"))
        dimensions = []
        for name in dims_raw:
            name = str(name).strip()
            if name and name not in _dim_names(dimensions):
                dimensions.append(DimensionSpec(name=name, source="llm"))
        # LLM 未产出要素时：规则关键词兜底（保证 N1 有可用要素）
        if not metrics:
            for name, agg in _rule_metrics(sq):
                metrics.append(MetricSpec(name=name, agg=agg, source="rule"))
        if not dimensions:
            for name in _rule_dimensions(sq):
                dimensions.append(DimensionSpec(name=name, source="rule"))

        # 指标/维度缺失 → 低置信度（前端预览高亮提示）
        if not metrics and not dimensions:
            confidence = min(confidence, 0.3)

        time = _extract_time(sq, parent_time)

        title = str(item.get("title") or "").strip() or _fallback_title(sq)

        specs.append(SubQuerySpec(
            sub_id=sub_id,
            question=sq.strip(),
            intent=intent,
            metrics=metrics,
            dimensions=dimensions,
            time=time if time else TimeSpec(),
            schema_name=schema_name,
            table_hints=hints,
            chart_hint=str(item.get("chart_hint") or "") or None,
            title=title,
            confidence=confidence,
            enabled=True,
        ))

    logger.info("[多查询][要素补齐] %d 个子查询要素提取完成（LLM=%s）",
                len(specs), bool(raw))
    return specs


def _llm_extract(question: str, sub_questions: list[str], llm: Any) -> list[dict] | None:
    """LLM 批量提取要素（一次调用）；失败返回 None。"""
    try:
        prompt = (
            "你是数据分析查询要素提取助手。以下问题被拆解为多个子查询，"
            "请为每个子查询提取结构化要素。\n"
            "【输出要求】严格返回 JSON 数组，每个元素对应一个子查询：\n"
            '[{"intent": "value|compare|ranking|trend|detail|statistic", '
            '"metrics": ["指标名"], "dimensions": ["维度名"], '
            '"title": "卡片标题", "chart_hint": "kpi|bar|line|group_bar|rank|pie|combo"}]\n'
            "【标题生成规则】\n"
            "1. title 是图表卡片的展示标题，必须由你生成，禁止留空，禁止直接复制子查询原文\n"
            "2. 标题应简洁概括该图表的核心内容（6-14字），包含关键时间范围（如近7日/本月）和核心指标\n"
            "3. 去掉查询/统计/哪个/的记录/分别是等无意义填充词，去掉重复的业务对象名（多个子查询同一对象时省略）\n"
            "4. 标题应体现洞察角度而非查询动作：趋势类用「XX趋势/每日XX」，排行类用「XX排行/XX最多」，"
            "占比类用「XX占比/XX前三占比」，对比类用「XX对比」\n"
            "5. 示例：子查询「查询近7日的审批流程每日发起量」→ title「近7日每日发起量」；"
            "子查询「流程近7天发起占比最多的流程是哪个流程」→ title「近7日发起最多的流程」；"
            "子查询「用饼图统计各审批流程发起量前三的占比记录」→ title「近7日流程数量前三占比」\n"
            "【规则】\n"
            "1. 时间范围等公共条件不写入 metrics/dimensions\n"
            "2. metrics 指数值类统计口径（销售额/数量/占比等），dimensions 指分组分类口径（区域/门店/日期等）\n"
            "3. 无法确定时字段留空数组，不要编造\n"
            "4. 只输出 JSON 数组，不要其他文字\n"
            "5. 同一原始问题拆出的多个子查询，时间口径必须一致：统一使用同一时间字段，禁止各子查询混用不同时间字段导致口径不一致\n"
            "6. chart_hint：若子查询文本中用户明确要求某种图表（如饼图/折线图/柱状图/排行榜/KPI/雷达图等），"
            "chart_hint 必须取对应值（pie/line/bar/rank/kpi/radar），覆盖默认意图推断；"
            "用户未明确要求时才按意图默认（ranking→rank、trend→line、statistic→bar 等）\n\n"
            f"原始问题：{question}\n子查询列表：\n"
            + "\n".join(f"- {q}" for q in sub_questions)
        )
        resp = llm.chat([{"role": "user", "content": prompt}],
                        temperature=0.1, json_mode=True, thinking=False)
        text = resp if isinstance(resp, str) else getattr(resp, "content", str(resp))
        return _parse_json_array(text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[多查询][要素补齐] LLM 提取失败: %s", exc)
        return None


def _parse_json_array(text: str) -> list[dict] | None:
    """宽松解析 LLM 返回的 JSON 数组。

    兼容：markdown 代码块包裹、元素间缺逗号（`} {`）、尾逗号、前后说明文字。
    仍失败时退化为逐对象解析，保证单元素可用。
    """
    if not text:
        return None
    m = re.search(r"\[([\s\S]*)\]", text)
    if m:
        body = m.group(1)
        # 修复常见 LLM 输出瑕疵：元素间缺逗号、尾逗号
        body = re.sub(r"\}\s*\{", "},{", body)
        body = re.sub(r",\s*\]", "]", body)
        try:
            data = json.loads("[" + body + "]")
            return data if isinstance(data, list) else None
        except Exception:  # noqa: BLE001
            pass
    # 逐对象解析（保底）：取所有 {…} 块逐个 parse
    out: list[dict] = []
    for obj in re.findall(r"\{[\s\S]*?\}", text):
        try:
            parsed = json.loads(obj)
            if isinstance(parsed, dict):
                out.append(parsed)
        except Exception:  # noqa: BLE001
            continue
    return out or None
