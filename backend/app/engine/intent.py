"""L2 意图理解层：意图分诊 → 6 大业务意图分类 → QuerySpec 结构化参数抽取。

识别策略（三层递进）：
1. 规则引擎（快路径）：意图信号词典 + Schema 实体匹配 + 时间解析，确定性输出 QuerySpec；
2. LLM 结构化抽取（兜底路径）：规则未命中关键要素时，LLM 按 JSON Schema 输出 QuerySpec；
3. 歧义消歧与兜底：低置信度拦截 / 参数缺失澄清 / 非数据问题拦截。

QuerySpec 是全链路唯一事实来源（见 engine/query_spec.py）。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta

from ..llm import LLMError, parse_json_content
from ..models import Datasource, TableMeta
from .mapping import (MappingError, fetch_schema_candidates, map_spec_to_schema)

from .query_spec import (INTENT_LABELS, QuerySpec, MetricSpec, DimensionSpec,
                         BucketSpec, FilterSpec, TimeSpec, ActionSpec,
                         extract_schema_expr, extract_time_expr,
                         parse_time_expr, spec_from_dict, spec_to_dict)

from .text_utils import tokenize, contains_term
from .schema_types import is_numeric, is_date, is_system_field, is_id_field
from .biz_lexicon import match as lex_match, get as lex_get
from .llm_json import ask as llm_json_ask

logger = logging.getLogger(__name__)

# ========== 第一级：意图分诊（chat / knowledge / data） ==========
TRIAGE_PROMPT = """你是一个精准的意图分类器。根据用户问题和对话历史，判断唯一意图类型：

【分类定义】
- chat：闲聊、打招呼、文章创作、翻译、写作、编程、通用问答、脑筋急转弯、诗歌、故事、笑话等不依赖企业数据库和业务知识库的任务
- knowledge：业务知识、规则、流程、制度、操作指南、FAQ 问答（需要检索企业知识库回答，如"XX流程是什么""报销规则"）
- data：查询或分析数据库中的业务数据（数值查询、对比、榜单、趋势、明细、统计、分布，如"本月销售额""各渠道营收占比""近30天订单量趋势""文件大小范围分布"）

【判断要点】
- 出现"统计/分析/趋势/占比/对比/环比/同比/TOP/排名/分布/销售额/订单/用户/销量/营收/金额/数量/大小"等且指向业务数据 → data
- **以"查询/查/统计/看下/分析/列出"等动作开头，且后面跟着具体数据对象（文件/表/记录/报表/明细/数量/金额等）→ data，即使句中含"流程/审批/规则"等限定词**（如"查询审批流程上传文件的大小"→ 要查的是文件数据，不是流程知识）
- 出现"流程/规则/制度/怎么办/如何/什么是"且**提问对象是知识本身**（如"审批流程是什么""报销流程怎么走"）→ knowledge
- 不依赖数据的创作、闲聊、通用知识 → chat
- 多轮追问中"再按月份拆分""环比去年呢"等基于上一次查询的分析 → data

只输出 JSON：{"intent": "chat|knowledge|data"}
【重要】intent 字段只能是 chat、knowledge、data 三个单词中的一个，不要输出竖线、斜杠、中文或枚举列表。"""


# ========== 问题重构（意图识别后、QuerySpec 解析前） ==========
REWRITE_PROMPT = """你是企业数据问数的问题重构器。用户的问题可能口语化、有歧义、夹带知识库词或负向澄清，请把它改写成「清晰、聚焦、可执行」的规范化问数描述，并提炼结构要素。只输出 JSON。

【改写原则】
1. 目标唯一：写清「统计对象 + 统计维度 + 统计方式」，删除"帮我看看/能不能/不需要统计X/只需…"等冗余与负向表达（负向信息进 negatives，不进 question）
2. 实体明确：识别用户所指的业务对象（表/实体）。如"审批流程上传的文件"→ 实体是「审批流程的文件」（要查文件数据，不是查流程知识）
3. 区分知识词与数据实体："流程/审批/规则"等词出现时，判断它是业务对象限定词（如"审批流程的文件"）还是知识问答（如"审批流程是什么"）；数据问数一律写实体，不写知识词
4. **拆表（table_hints）**：输出 3-6 个表名/实体检索词，用于后续表检索。**必须同时包含**：① 用户原话中的业务实体词（如"审批流程""文件"——参考 FAQ 意图识别的 keywords 提取）；② 按「业务词 + 常见后缀」推导的表名词（如"审批流程上传的文件"→ 审批文件、审批附件、文件附件、流程附件）。必须来自用户原话中的业务概念，不得凭空发明
5. 保留派生维度："大小范围/金额区间/年龄段/时间段"等是字段分箱（bucket）维度，必须显式保留在 dimension_hint，不得丢弃
6. 保留时间与对比：近7天/本月/同比/环比/去年等必须原样保留在 question 中
7. 不得编造：改写只能用用户原话中的概念，不得发明字段名或数值

【输出 JSON Schema】
{{
  "question": "规范化问数描述（一句话，如：查询审批流程的文件表，统计维度：文件大小范围，统计方式：数量分布）",
  "entities": ["业务实体名"],
  "table_hints": ["表检索词1", "表检索词2"],
  "intent_hint": "value|compare|ranking|trend|detail|statistic",
  "metric_hint": "要统计的量（如：数量/金额/平均值），没有写空串",
  "dimension_hint": ["分组维度（含范围类派生维度）"],
  "filters_hint": ["筛选条件，没有则空数组"],
  "negatives": ["用户明确排除的内容"]
}}

【示例】
问题：查询审批流程上传文件的大小，需要文件大小范围的分布图。不需要统计流程，只需要统计文件大小的分布
输出：
{{"question": "查询审批流程的文件表，统计维度：文件大小范围，统计方式：数量分布",
 "entities": ["审批流程文件"],
 "table_hints": ["审批文件", "审批附件", "文件附件", "流程附件"],
 "intent_hint": "statistic",
 "metric_hint": "数量",
 "dimension_hint": ["文件大小范围"],
 "filters_hint": [],
 "negatives": ["不统计流程数量"]}}"""


def rewrite_question(question: str, llm) -> dict | None:
    """问题重构：把口语/歧义/夹带负向澄清的问题改写为规范化问数描述。

    返回 {"question": 规范化描述, "entities": [...], "table_hints": [...],
          "intent_hint": ..., "metric_hint": ..., "dimension_hint": [...],
          "filters_hint": [...], "negatives": [...]}；
    table_hints 为拆表检索词（供选表阶段表名/表注释检索）；
    失败返回 None（上层回退原始问题）。
    """
    if not question or not question.strip():
        return None
    try:
        raw = llm.chat([
            {"role": "system", "content": REWRITE_PROMPT},
            {"role": "user", "content": f"【用户问题】\n{question}\n\n请输出上述 JSON Schema 的完整 JSON。"},
        ], max_tokens=800, temperature=0.1, thinking=False)
        data = parse_json_content(raw)
    except (LLMError, Exception) as exc:  # noqa: BLE001
        logger.warning("问题重构失败，回退原始问题: %s", exc)
        return None
    if not isinstance(data, dict) or not str(data.get("question") or "").strip():
        return None
    q = str(data["question"]).strip()
    # 防御：question 字段损坏（LLM 把 JSON 键名/多余字段串进 question 值）→ 回退原始问题，
    # 避免脏文本污染下游规则引擎/LLM 兜底解析（曾出现 "查询istic metric_hint=数量…" 脏串）
    _json_key_marker = re.compile(r"\b(metric_hint|dimension_hint|intent_hint|filters_hint|negatives|entities|table_hints|intent|question)\b")
    if (_json_key_marker.search(q)
            or q.count('"') > 4 or len(q) > 150):
        logger.warning("[问数][rewrite] 问题重构返回的 question 字段损坏，回退原问题: %r", q[:60])
        q = question.strip()
    # 拆表检索词：LLM 推导表名词 + 原话实体词（entities）+ 问题提取词 融合去重
    # 参考 UnifiedQA：keywords 直接取原话业务实体（如"审批流程""文件"），
    # 与推导表名词（"审批文件""流程附件"）合并，四维检索时两类词都能命中表
    hints = data.get("table_hints") or []
    if not isinstance(hints, list):
        hints = []
    table_hints = [str(h).strip() for h in hints if h and str(h).strip()]
    for ent in (data.get("entities") or []):
        e = str(ent).strip()
        if e and e not in table_hints:
            table_hints.append(e)
    # 从原问题提取原话业务实体词兜底/补充（参考 UnifiedQA keywords：如"审批流程""文件"）
    # 每处业务关键词只提取一个最自然词：优先「关键词+后随业务字」（审批+流程→审批流程），
    # 否则取关键词本身；避免"查询审批流程上传"类碎词
    _business_kw = ("文件", "审批", "附件", "订单", "流程", "项目", "用例", "任务",
                    "记录", "数据", "报告", "薪资", "考勤", "部门", "员工", "接口", "用户")
    _tail_stop = ("的", "大", "上", "传", "查", "询", "看", "下", "了", "吗", "呢",
                  "请", "帮", "我", "是", "要", "想", "不", "只", "统计", "分布", "范围")
    for m in re.finditer("|".join(_business_kw), question):
        if len(table_hints) >= 8:
            break
        kw = m.group()
        tail_m = re.match(r"[\u4e00-\u9fa5]{1,2}", question[m.end():m.end() + 2])
        tail = tail_m.group(0) if tail_m else ""
        if tail and all(ch in _tail_stop for ch in tail):
            tail = ""  # 后随"的/大/上传"等非业务字 → 不吞（"文件的大"→"文件"）
        cand = (kw + tail)[:4]
        if cand not in table_hints:
            table_hints.append(cand)
    table_hints = table_hints[:8]
    data["table_hints"] = table_hints
    data["question"] = q
    if q == question.strip():
        # 模型未改写（已足够规范）：不覆盖原始问题，避免画蛇添足
        return {"question": q, "entities": [], "table_hints": table_hints,
                "intent_hint": "", "metric_hint": "", "dimension_hint": [],
                "filters_hint": [], "negatives": []}
    return data


def detect_intent(question: str, history: list[dict], llm) -> str:
    """意图分诊：返回 chat/knowledge/data。无 LLM 时默认 data。"""
    if llm is None or not llm.configured:
        return "data"
    hist_text = ""
    if history:
        recent = history[-4:]
        parts = []
        for m in recent:
            role = "用户" if m.get("role") == "user" else "助手"
            text = m.get("text", "")
            if text:
                parts.append(f"{role}：{text}")
        hist_text = "\n".join(parts)
    user_prompt = f"【对话历史】\n{hist_text}\n\n【当前问题】{question}\n\n请输出意图分类 JSON。"
    try:
        raw = llm.chat([
            {"role": "system", "content": TRIAGE_PROMPT},
            {"role": "user", "content": user_prompt},
        ], max_tokens=400, temperature=0.1, thinking=False)
        result = parse_json_content(raw)
        intent = (result.get("intent") or "").strip().lower()
        if intent in ("chat", "knowledge", "data"):
            return intent
        # 模型偶发回显枚举串（如 "chat|knowledge|data" / "chat,data"），按问题信号兜底
        logger.warning("意图分诊返回未知值: %s，按问题信号兜底", intent)
        q = question or ""
        # 数据信号优先：查询/统计动作 + 具体数据对象 → data（即使含"流程/审批/规则"限定词）
        if any(k in q for k in ("查询", "查一下", "查查", "统计", "分析", "数据", "报表",
                                "多少", "分布", "占比", "趋势", "数量", "金额", "销量",
                                "营收", "环比", "同比", "上月", "本月", "上周", "本周",
                                "排名", "明细", "列表", "合计", "均值", "大小", "文件",
                                "记录", "明细", "清单")):
            return "data"
        if any(k in q for k in ("流程", "规则", "制度", "怎么办", "如何", "指南", "步骤",
                                "操作", "什么是", "报销", "审批")):
            return "knowledge"
        return "chat"
    except (LLMError, Exception) as exc:  # noqa: BLE001
        logger.warning("意图分诊失败: %s，默认 data", exc)
        return "data"


# ========== 第二级：6 大业务意图信号词典 ==========
_INTENT_SIGNALS: dict[str, list[tuple[str, int]]] = {
    "compare": [("同比增长", 4), ("环比增长", 4), ("同比下降", 4), ("环比下降", 4),
                ("同比", 3), ("环比", 3), ("同期", 3), ("对比", 3), ("比较", 3), ("较", 1),
                ("vs", 2), ("涨", 2), ("跌", 2), ("增长", 2), ("下降", 2),
                ("增加", 1), ("减少", 1), ("高于", 2), ("低于", 2)],
    "ranking": [("排名", 3), ("排行", 3), ("最多", 3), ("最少", 3), ("最高", 3),
                ("最低", 3), ("top", 2), ("TOP", 2), ("榜", 2)],
    "trend": [("增长趋势", 4), ("变化趋势", 4), ("趋势", 3), ("走势", 3), ("变化", 3),
              ("逐月", 3), ("逐日", 3), ("逐周", 3), ("按月", 3), ("按日", 3),
              ("按周", 3), ("分月", 3), ("分日", 3), ("时间分布", 4)],
    "detail": [("明细", 3), ("列表", 3), ("清单", 3), ("列出", 3), ("哪些", 2),
               ("名单", 3), ("记录", 2), ("详细", 2), ("存在", 3), ("有哪些", 3),
               ("有什么", 2), ("查一下", 2), ("查查", 2)],
    "statistic": [("占比", 4), ("比例", 3), ("构成", 3), ("合计", 2), ("总计", 2),
                  ("平均", 2), ("均值", 3), ("分布", 3), ("分层", 3),
                  ("最大值", 3), ("最小值", 3)],
}

_TOP_N_RE = re.compile(r"(?:前|后|top|TOP)\s*(\d{1,3})")
_CN_TOP_N_RE = re.compile(r"(?:前|后)([一二两三四五六七八九十百千]+)")
_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6,
           "七": 7, "八": 8, "九": 9, "十": 10, "百": 100, "千": 1000}


def _cn_to_int(s: str) -> int:
    total, cur = 0, 0
    for ch in s:
        if ch in "一二两三四五六七八九":
            cur = _CN_NUM[ch]
        elif ch in "十百千":
            if ch == "十" and cur == 0:
                cur = 1
            total += cur * _CN_NUM[ch]
            cur = 0
    return total + cur
_RANK_N_RE = re.compile(r"第[一二三四五六七八九十百\d]+")

_FILTER_PATTERNS = [
    (re.compile(r"([\u4e00-\u9fa5]{2,6})(?:为|是|=)([\u4e00-\u9fa5]{1,8})"), "="),
    (re.compile(r"(?:大于|超过|高于|>)\s*(\d[\d,.]*)"), ">"),
    (re.compile(r"(?:小于|低于|<)\s*(\d[\d,.]*)"), "<"),
    (re.compile(r"(?:不低于|>=\s*|≥)\s*(\d[\d,.]*)"), ">="),
    (re.compile(r"(?:不高于|<=\s*|≤)\s*(\d[\d,.]*)"), "<="),
    (re.compile(r"未([\u4e00-\u9fa5]{1,6})"), "!="),
]

# 明细指标字符级检查（biz_lexicon 管理词表，此处仅保留字符级判断逻辑）
_DETAIL_METRIC_CHAR = ("数", "量", "额", "率", "价")


def _detect_intent(question: str, action: ActionSpec) -> str:
    q = question.lower()
    scores: dict[str, int] = {}
    # 前N/后N（含中文数字）是榜单的强信号
    if _TOP_N_RE.search(question) or _CN_TOP_N_RE.search(question):
        scores["ranking"] = scores.get("ranking", 0) + 3
    for intent, signals in _INTENT_SIGNALS.items():
        s = 0
        for kw, w in signals:
            if kw in q:
                s += w
        if s:
            scores[intent] = s
    if scores:
        best = max(scores, key=scores.get)
        if scores[best] >= 2:
            # 排名：提取 TopN（支持 阿拉伯数字 与 中文数字：前五/前十/前二十）
            if best == "ranking":
                m = _TOP_N_RE.search(question)
                if m:
                    action.top_n = min(int(m.group(1)), 100)
                else:
                    m = _CN_TOP_N_RE.search(question)
                    if m:
                        n = _cn_to_int(m.group(1))
                        action.top_n = min(n, 100) if n > 0 else None
                if action.top_n:
                    action.sort = "desc"
            return best
    # 默认：按时间/维度信号回退
    if any(k in q for k in ("多少", "几个", "什么", "是多少", "有几", "总", "多少元", "金额")):
        return "value"
    return "value"


def _extract_time(question: str, spec: QuerySpec) -> bool:
    expr = extract_time_expr(question)
    if expr:
        ts = parse_time_expr(expr)
        if ts:
            spec.time = ts
            return True
    # 明细/清单类：用户未指定时间时不加默认时间过滤（避免污染清单结果）
    if spec.intent == "detail":
        spec.time = TimeSpec()
        return False
    # 纯计数类（COUNT / DISTINCT_COUNT）：用户问"有多少个X/ X的数量/ X总数"时问的是当前总量，不加默认时间过滤
    if spec.metrics and all(m.agg in ("count", "distinct_count") for m in spec.metrics):
        spec.time = TimeSpec()
        return False
    # 默认口径：近 30 天（口径在结果中明示）
    today = datetime.now().date()
    spec.time = TimeSpec(
        type="range", expr="近30天（默认）",
        start=(today - timedelta(days=29)).strftime("%Y-%m-%d"),
        end=today.strftime("%Y-%m-%d"), granularity="day")
    return False


def _entity_score(question: str, item: dict) -> int:
    q = question.lower()
    text = ((item.get("comment") or "") + " " + (item.get("column") or "")).lower()
    if not text.strip():
        return 0
    score = 0
    for seg in re.findall(r"[\u4e00-\u9fa5]{2,}", text):
        if len(seg) >= 2 and seg in q:
            score = max(score, 10 + len(seg))
    for seg in re.findall(r"[a-z0-9_]{2,}", text):
        if seg in q:
            score = max(score, 8 + len(seg))
    if score == 0:
        for seg in re.findall(r"[\u4e00-\u9fa5]{3,}", q):
            if seg in text:
                score = max(score, 5)
    return score


def _entity_segments(question: str, item: dict) -> list[str]:
    """返回 item 注释/字段名中出现在问题里的片段（按长度降序）。"""
    q = question.lower()
    text = ((item.get("comment") or "") + " " + (item.get("column") or "")).lower()
    segs = set()
    for seg in re.findall(r"[\u4e00-\u9fa5]{2,}", text):
        if len(seg) >= 2 and seg in q:
            segs.add(seg)
    for seg in re.findall(r"[a-z0-9_]{2,}", text):
        if seg in q:
            segs.add(seg)
    return sorted(segs, key=len, reverse=True)


def _extract_metrics(question: str, candidates: dict,
                     metric_dicts: list[dict]) -> tuple[list[MetricSpec], set[str]]:
    matched: list[tuple[int, str, dict]] = []
    for m in candidates["metrics"]:
        s = _entity_score(question, m)
        if s >= 8:
            matched.append((s, m["column"], m))
    # 词典命中（可指定 target 字段）
    for d in metric_dicts:
        hit = d["term"] in question or any(a and a in question for a in d.get("aliases", []))
        if hit and d.get("target_column"):
            matched.append((10, d["target_column"], {
                "table": d.get("target_table", ""), "column": d["target_column"],
                "comment": d["term"], "data_type": "", "agg": d.get("agg", "")}))
    # 计数语境（次数/多少个/调用量/X数+多少 等）下，ID/键类字段（如"项目ID"）几乎都是分组或过滤键，
    # 禁止被词典/实体匹配误配成 SUM 指标（典型："RT项目中每个接口被调用了多少次"→项目ID sum；
    # "RT项目的测试用例数分别是多少"→项目ID/用例ID/关联ID sum）
    if _is_count_context(question):
        matched = [(s, col, m) for s, col, m in matched
                   if not ((m.get("column") or "").endswith("_id")
                           or "ID" in (m.get("comment") or "")
                           or (m.get("column") or "") in ("id",))]
    # 去重：同一指标名（注释）只保留一个（多张表同字段时按最高分取优）
    seen: dict[str, tuple[int, dict]] = {}
    for s, col, m in matched:
        key = m.get("comment") or m.get("column") or col
        if key not in seen or s > seen[key][0]:
            seen[key] = (s, m)
    ordered = sorted(seen.values(), key=lambda x: x[0], reverse=True)[:3]
    out: list[MetricSpec] = []
    tables: set[str] = set()
    for s, m in ordered:
        agg = m.get("agg") or "sum"
        if m.get("table"):
            tables.add(m["table"])
        out.append(MetricSpec(name=(m.get("comment") or m.get("column") or ""),
                              alias=m.get("column") or "", agg=agg,
                              unit="", source="rule"))
    return out, tables


def _extract_dimensions(question: str, candidates: dict, dim_dicts: list[dict],
                        metric_terms: list[str], metric_tables: set[str]) -> list[DimensionSpec]:
    matched: list[tuple[int, str, dict]] = []
    for d in candidates["dimensions"]:
        segs = _entity_segments(question, d)
        if not segs:
            continue
        # 维度匹配片段若与指标词重合（如"重试次数"出现于执行配置注释、或"重试"是"重试次数"的子串），
        # 视为误配，跳过
        if any(any(seg in mt or mt in seg for mt in metric_terms if mt) for seg in segs):
            continue
        score = max(10 + len(s) for s in segs)
        if d.get("table") in metric_tables:
            score += 10  # 与指标同表优先（保证单表模板 SQL 可走）
        # 名称类字段（X名称/name）作为维度语义更准（如 接口名称/用例名称），
        # 避免"API格式:...-Chat Completions接口"这类描述型注释被"接口"半词命中
        if (d.get("column") or "").endswith("name") or (d.get("comment") or "").endswith("名称"):
            score += 8
        if score >= 10:
            matched.append((score, segs[0], d))
    for dd in dim_dicts:
        hit = dd["term"] in question or any(a and a in question for a in dd.get("aliases", []))
        if hit and dd.get("target_column") and dd["term"] not in metric_terms:
            matched.append((10, dd["term"], {
                "table": dd.get("target_table", ""), "column": dd["target_column"],
                "comment": dd["term"]}))
    seen: dict[str, tuple[int, dict]] = {}
    for s, seg, d in matched:
        if seg not in seen or s > seen[seg][0]:
            seen[seg] = (s, d)
    ordered = sorted(seen.values(), key=lambda x: x[0], reverse=True)[:4]
    return [DimensionSpec(name=(d.get("comment") or d.get("column") or ""),
                          alias=d.get("column") or "", source="rule")
            for s, d in ordered]


def _extract_filters(question: str, candidates: dict) -> list[FilterSpec]:
    filters: list[FilterSpec] = []
    # 阈值型：金额大于1000 → 匹配到数值字段
    for pat, op in _FILTER_PATTERNS:
        for m in pat.finditer(question):
            g = m.groups()
            if not g:
                continue
            if op in (">", "<", ">=", "<="):
                field_name = question[max(0, m.start() - 6):m.start()].strip(" ，。与的")
                value = float(g[0].replace(",", ""))
                # 从问题中找字段词：优先最后一个中文实体
                field = _match_filter_field(field_name, candidates)
                if field:
                    filters.append(FilterSpec(field=field_name or field["comment"],
                                              op=op, value=value, source="rule"))
            else:
                field_name = (g[0] if len(g) >= 2 else "") or ""
                value = g[-1]
                if op == "!=" and len(g) == 1:
                    value = g[0]
                if field_name and value:
                    field = _match_filter_field(field_name, candidates)
                    if field:
                        filters.append(FilterSpec(field=field_name, op=op, value=value,
                                                  source="rule"))
    # 状态词：已完成/未付款等 → 匹配维度字段
    for w in lex_get("status"):
        if w in question:
            field = _match_filter_field("状态", candidates) or _match_filter_field("类型", candidates)
            if field:
                filters.append(FilterSpec(field=field["comment"] or "状态",
                                          op="!=" if w.startswith("未") else "=",
                                          value=w.lstrip("未"), source="rule"))
                break
    return filters


def _match_filter_field(name: str, candidates: dict) -> dict | None:
    """条件字段映射：优先维度字段注释包含 名称；其次任意字段。"""
    if not name:
        return None
    for d in candidates["dimensions"]:
        text = ((d.get("comment") or "") + d.get("column", "")).lower()
        if name and (name.lower() in text or any(
                w in text for w in re.findall(r"[\u4e00-\u9fa5]{2,}", name))):
            return d
    for m in candidates["metrics"]:
        text = ((m.get("comment") or "") + m.get("column", "")).lower()
        if name and name.lower() in text:
            return m
    return None


# ========== LLM 兜底抽取 ==========
SPEC_EXTRACT_PROMPT = """你是企业数据问数意图解析器。把用户问题解析为结构化查询参数 JSON，只输出 JSON。

【可选指标】(名称对应数据字段，agg 可选 sum/count/avg/max/min/distinct_count)
{metrics_hint}

【可选维度】(用于分组)
{dims_hint}

【可选时间字段】
{times_hint}

【JSON Schema】
{{
  "intent": "value|compare|ranking|trend|detail|statistic",
  "metrics": [{{"name": "指标名(用上面可选中英文名或注释)", "agg": "sum"}}],
  "dimensions": [{{"name": "维度名", "bucket": {{"field": "分箱字段名(可省)", "ranges": [[下界,上界],...], "labels": ["区间标签",...], "unit": "单位"}}}}],
  "filters": [{{"field": "条件字段", "op": "=/!=/>/</>=/<=", "value": 值}}],
  "time_expr": "时间表达原文，如 本月/近7天/2026年8月，没有则留空",
  "action": {{"top_n": null, "sort": null, "compare_target": null, "stat": null}}
}}

【规则】
- intent：趋势/逐月→trend；同比/环比/对比→compare；排名/前N→ranking；明细/列表→detail；占比/比例/构成→statistic；**每个/各个/分别/各 + 维度 + 数量/数值（分组统计，如"各项目用例数分别是多少"）→statistic；范围/区间/分布（如"文件大小范围的分布"）→statistic**；其余单值数值→value
- 指标和维度必须从【可选指标】【可选维度】中选择（可用中文注释或英文字段名），禁止编造
- **分箱维度（bucket）**：仅当维度语义是"范围/区间"（大小范围/金额区间/年龄段/时长区间）时输出 bucket；ranges 用合理业务边界（按用户原话或常见分档），labels 与 ranges 一一对应（如 ["0-1MB","1-10MB","10MB以上"]），unit 填单位；字段不在此列可留空 field
- **禁止用 bucket 表达时间粒度**：每天/每周/每月/按天/按周/按月等时间分组，用 time_expr 表达时间范围（如 近7天/本月），日期分组由查询逻辑处理；bucket.ranges 只能是数值区间，严禁写入日期（如 ["2023-01-01","2023-12-31"]）
- compare 时 action.compare_target：环比=mom、同比=yoy、维度对比=dim
- 用户问题若未提及时间，time_expr 留空
- 多轮追问（如"环比去年呢""按渠道拆分呢"）时，依据上一轮 QuerySpec 继承指标/维度，只改时间或加维度"""


def _llm_extract_spec(question: str, datasource_id: int, llm,
                      prev_spec: QuerySpec | None,
                      schema_name: str | None = None,
                      rewrite_hint: dict | None = None,
                      confirmed_tables: list[str] | None = None) -> dict | None:
    candidates = fetch_schema_candidates(datasource_id, schema_name, confirmed_tables)
    schema_scope = f"【查询范围 schema】{schema_name}" if schema_name else ""
    # 问题实体相关表：问题重构的拆表检索词命中表/表注释 → 字段前置并标注【相关】，
    # 引导 LLM 优先从问题实体对应的表中选择指标/维度（防止选中无关表的字段，如"会议"误配审批字段）
    table_hints = [h for h in ((rewrite_hint or {}).get("table_hints") or []) if h]
    related = set()
    if table_hints:
        for bucket in ("metrics", "dimensions", "time_fields"):
            for it in candidates.get(bucket, []):
                t = it.get("table") or ""
                tc = it.get("table_comment") or ""
                if any(h and (h in t or h in tc) for h in table_hints):
                    related.add(t)
    def _fmt(it: dict) -> str:
        tag = "【相关】" if (it.get("table") or "") in related else ""
        return f"{tag}{it['comment'] or it['column']}({it['column']}, {it['table']})"
    metrics_hint = "；".join(_fmt(m) for m in candidates["metrics"][:30]) or "（无）"
    dims_hint = "；".join(_fmt(d) for d in candidates["dimensions"][:20]) or "（无）"
    times_hint = "；".join(_fmt(t) for t in candidates["time_fields"][:10]) or "（无）"
    prev_text = ""
    if prev_spec:
        prev_text = f"\n【上一轮 QuerySpec】{json.dumps(spec_to_dict(prev_spec), ensure_ascii=False)}"
    # 问题重构的提炼结果作为软提示注入（仅首轮），帮助 LLM 聚焦统计对象/维度/方式
    hint_text = ""
    if rewrite_hint:
        hint_parts = []
        if rewrite_hint.get("intent_hint"):
            hint_parts.append(f"意图参考={rewrite_hint['intent_hint']}")
        if rewrite_hint.get("metric_hint"):
            hint_parts.append(f"统计量参考={rewrite_hint['metric_hint']}")
        if rewrite_hint.get("dimension_hint"):
            hint_parts.append(f"分组维度参考={'、'.join(rewrite_hint['dimension_hint'])}")
        if rewrite_hint.get("filters_hint"):
            hint_parts.append(f"筛选条件参考={'、'.join(str(x) for x in rewrite_hint['filters_hint'])}")
        if hint_parts:
            hint_text = f"\n【问题重构提示（供参考，指标/维度仍需从可选列表选择）】\n{'；'.join(hint_parts)}"
    # 选择规则：标注【相关】的表与问题实体最匹配，必须优先从中选择；确认表时只允许选择确认表的字段
    select_note = ""
    if related:
        select_note = (f"\n【选择规则】标注【相关】的字段来自与问题最相关的表"
                       f"（{'、'.join(sorted(related))}），指标与维度必须优先从中选择；"
                       "禁止选择与问题实体无关的其他表字段。")
    elif confirmed_tables:
        select_note = (f"\n【选择规则】用户已确认查询表：{'、'.join(confirmed_tables)}，"
                       "指标与维度只能从这些表的字段中选择。")
    user_prompt = (f"{schema_scope}\n{prev_text}\n{hint_text}\n{select_note}\n\n"
                   f"【用户问题】{question}\n\n"
                   f"请输出上述 JSON Schema 的完整 JSON。")
    system = SPEC_EXTRACT_PROMPT.format(
        metrics_hint=metrics_hint, dims_hint=dims_hint, times_hint=times_hint)
    data = llm_json_ask(llm, system, user_prompt, max_tokens=1600, max_retries=1)
    return data if isinstance(data, dict) else None


def _is_junk_field_name(name: str) -> bool:
    """过滤 LLM 兜底解析混入的脏字段名（schema 引导文本/超长串），避免污染指标与维度。"""
    n = (name or "").strip()
    if not n or len(n) > 20:
        return True
    if re.search(r"(维度\s*0|指标\s*[0-9]|模板\s*[0-9]|全局\s*[0-9])", n):
        return True
    return False


def _spec_from_llm_data(data: dict, question: str, prev_spec: QuerySpec | None) -> QuerySpec | None:
    try:
        intent = data.get("intent") or "value"
        if intent not in INTENT_LABELS:
            intent = "value"
        metrics = [MetricSpec(name=m.get("name", ""), agg=m.get("agg", "sum"), source="llm")
                   for m in (data.get("metrics") or [])
                   if m.get("name") and not _is_junk_field_name(m["name"])]
        dims = []
        for d in (data.get("dimensions") or []):
            if not d.get("name") or _is_junk_field_name(d["name"]):
                continue
            bucket = None
            b = d.get("bucket") or {}
            if isinstance(b, dict) and b.get("ranges"):
                try:
                    _num_ranges = [[float(x), float(y)] for x, y in b["ranges"]]
                except (TypeError, ValueError):
                    # LLM 常把"每天/按天/近7天"误写成日期区间 bucket（bucket 仅用于数值分箱）：
                    # 丢弃 bucket 保留维度，避免整个 Spec 因 BucketSpec 校验失败而作废
                    _num_ranges = None
                    logger.info("[问数][spec] LLM bucket.ranges 非数值（%r），忽略 bucket 保留维度 %s",
                                str(b["ranges"])[:60], d.get("name"))
                if _num_ranges:
                    bucket = BucketSpec(field=str(b.get("field") or ""),
                                        ranges=_num_ranges,
                                        labels=[str(x) for x in (b.get("labels") or [])],
                                        unit=str(b.get("unit") or ""))
            dims.append(DimensionSpec(name=d["name"], source="llm", bucket=bucket))
        filters = [FilterSpec(field=f.get("field", ""), op=f.get("op", "="),
                              value=f.get("value"), source="llm")
                   for f in (data.get("filters") or []) if f.get("field")]
        action_data = data.get("action") or {}
        action = ActionSpec(top_n=action_data.get("top_n"), sort=action_data.get("sort"),
                            compare_target=action_data.get("compare_target"),
                            stat=action_data.get("stat"))
        spec = QuerySpec(intent=intent, metrics=metrics, dimensions=dims,
                         filters=filters, action=action, confidence=0.8,
                         original_question=question)
        expr = (data.get("time_expr") or "").strip()
        if expr:
            ts = parse_time_expr(expr)
            if ts:
                spec.time = ts
        else:
            _extract_time(question, spec)
        # 继承上一轮（LLM 已自行合并时跳过重复继承）
        if prev_spec and not metrics:
            spec.metrics = prev_spec.metrics
            spec.inherited_from["metrics"] = True
        if prev_spec and not dims:
            spec.dimensions = prev_spec.dimensions
            spec.inherited_from["dimensions"] = True
        # 分组统计后处理：问题含"每个/各个/分别/各+维度"时强制 statistic
        # （LLM 易把"各项目用例数分别是多少"误判为 value，且可能漏提维度）
        _group_kw = ("每个", "各个", "分别", "各项目", "各渠道", "各地区", "各部门", "各类型", "各状态")
        if spec.intent == "value" and any(k in question for k in _group_kw):
            spec.intent = "statistic"
            if not spec.action.stat:
                spec.action.stat = "sum"
            # 维度为空时从问题中兜底提取（项目/渠道/地区等）
            if not spec.dimensions:
                for _dim_name in ("项目", "渠道", "地区", "部门", "类型", "状态", "产品", "用户"):
                    if _dim_name in question:
                        spec.dimensions = [DimensionSpec(name=_dim_name, source="rule")]
                        break
        missing = []
        if not spec.metrics and spec.intent != "detail":
            missing.append("指标")
        if not spec.time.start and spec.intent != "detail":
            missing.append("时间")
        spec.missing = missing
        return spec
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM QuerySpec 组装失败: %s", exc)
        return None


# ========== 规则引擎主流程 ==========
_COUNT_KW = (("多少次", "次数"), ("调用次数", "调用次数"), ("调用量", "调用次数"),
             ("被调用", "调用次数"), ("调用了", "调用次数"), ("访问量", "访问次数"),
             ("访问次数", "访问次数"), ("请求次数", "请求次数"), ("请求数", "请求次数"),
             ("执行次数", "执行次数"), ("命中次数", "命中次数"))

# "X数/数量"类指标（测试用例数、接口数、任务数量…）+ 数量问词 → 计数语境
_X_NUM_RE = re.compile(r"([一-龥]{1,10})数(?!据|值|控|学|码|字|组|理|量)")
# "X数量"（会议数量/用例数量）：X+数量 本身就是明确的计数指标名，不依赖数量问词
_X_NUM2_RE = re.compile(r"([一-龥]{1,10})数量")


def _is_count_context(question: str) -> bool:
    """是否计数语境：命中强计数词，或"X数/数量"类指标词 + 数量问词。"""
    if lex_match("count_context", question):
        return True
    if _X_NUM_RE.search(question) and lex_match("num_ask", question):
        return True
    return False


def _count_metric_name(question: str) -> str | None:
    """强计数词 → count 指标名；"X数+数量问词" → X数 count；否则返回 None。"""
    for kw, name in _COUNT_KW:
        if kw in question:
            return name
    m = _X_NUM_RE.search(question)
    if m and lex_match("num_ask", question):
        name = m.group(1) + "数"
        # 去掉"XX的"定语（"RT项目的测试用例数"→"测试用例数"）
        if "的" in name:
            name = name.split("的", 1)[-1]
        return name
    m2 = _X_NUM2_RE.search(question)
    if m2:
        name = m2.group(1) + "数量"
        # 去掉"XX的"定语（"会议的会议数量"→"会议数量"）
        if "的" in name:
            name = name.split("的", 1)[-1]
        return name
    return None


def _rule_extract_spec(question: str, candidates: dict,
                       dicts: dict[str, list[dict]]) -> QuerySpec:
    spec = QuerySpec(original_question=question)
    # 动作
    spec.action = ActionSpec()
    spec.intent = _detect_intent(question, spec.action)
    # 点名表名（如 "agent_tasks 总共有多少条"）：以该表本身为准，
    # 避免外键注释（"关联 agent_tasks.id（异步执行载体）"）把指标误配到其他表
    tbl_hits = []
    if candidates:
        seen_tbls = set()
        for bucket in ("metrics", "dimensions", "time_fields"):
            for it in candidates.get(bucket, []):
                t = it.get("table") or ""
                if t and t not in seen_tbls:
                    seen_tbls.add(t)
        ql = question.lower()
        for t in seen_tbls:
            if re.search(rf"(?<![a-z0-9_]){re.escape(t.lower())}(?![a-z0-9_])", ql):
                tbl_hits.append(t)
    count_sig = any(k in question for k in ("多少条", "几条", "总数", "总共有", "多少", "数量", "几条"))
    # 分组统计信号：每个/各个/分别/各 + 维度词，说明是按维度分组统计而非单值查询
    group_sig = any(k in question for k in ("每个", "各个", "分别", "各项目", "各渠道", "各地区", "各部门", "各类型", "各状态"))
    if tbl_hits and count_sig and not group_sig:
        spec.intent = "value"
        spec.metrics = [MetricSpec(name=f"{tbl_hits[0]} 总记录数",
                                   alias=f"__table__:{tbl_hits[0]}",
                                   agg="count", source="rule")]
        spec.dimensions = []
        spec.filters = []
        time_matched = _extract_time(question, spec)
    elif tbl_hits and count_sig and group_sig:
        # 分组统计（如"每个项目的用例数量分别是多少"）：走 statistic，保留分组维度
        spec.intent = "statistic"
        spec.action.stat = "sum"
        spec.metrics = [MetricSpec(name=f"{tbl_hits[0]} 记录数",
                                   alias=f"__table__:{tbl_hits[0]}",
                                   agg="count", source="rule")]
        # 分组维度交由后续 _extract_dimensions 从问题中提取（项目/渠道等）
        time_matched = _extract_time(question, spec)
        spec.dimensions = _extract_dimensions(
            question, candidates, dicts.get("dimension", []),
            metric_terms=[m.name for m in spec.metrics], metric_tables=set(tbl_hits))
        spec.filters = _extract_filters(question, candidates)
    else:
        # 实体（先提取指标，时间提取需依赖 metrics 判断是否为纯计数问题）
        metrics, metric_tables = _extract_metrics(question, candidates, dicts.get("metric", []))
        # 强计数词兜底（次数/调用量/访问量等）：未提取到指标时生成 count 指标，
        # 避免"项目ID sum"这类误配或 LLM 乱选指标
        if not metrics:
            cnt_name = _count_metric_name(question)
            if cnt_name:
                # __count__: 标记 → 表不确定，强制走 LLM 按问题确定统计来源表（防模板错表 COUNT）
                metrics = [MetricSpec(name=f"__count__:{cnt_name}", agg="count", source="rule")]
                metric_tables = set()
        spec.metrics = metrics
        # 时间（metrics 已填充，纯计数问题可正确跳过默认时间）
        time_matched = _extract_time(question, spec)
        spec.dimensions = _extract_dimensions(
            question, candidates, dicts.get("dimension", []),
            metric_terms=[m.name for m in metrics], metric_tables=metric_tables)
        spec.filters = _extract_filters(question, candidates)
    # 明细/清单类：查询主体词（项目/接口/用例等）会被误配为指标/维度，按强词过滤，
    # 展示字段交由 L3 LLM 字段检索从表结构中按问题选择（R7）
    if spec.intent == "detail":
        _detail_metric_kw = lex_get("detail_metric")
        _detail_dim_kw = lex_get("detail_dim")
        spec.metrics = [m for m in spec.metrics
                        if any(k in (m.name or "") for k in _detail_metric_kw)
                        or any(k in (m.name or "") for k in _DETAIL_METRIC_CHAR)]
        _ENUM_PAT = re.compile(r"[A-Za-z_]+-[^，,;；\s]+")
        spec.dimensions = [d for d in spec.dimensions
                           if any(k in (d.name or "") for k in _detail_dim_kw)
                           and len(_ENUM_PAT.findall(d.name or "")) < 2]
    # 统计子类型
    if spec.intent == "statistic":
        if "占比" in question or "比例" in question or "构成" in question:
            spec.action.stat = "ratio"
        elif "平均" in question or "均值" in question:
            spec.action.stat = "avg"
        elif "最大" in question or "最高" in question:
            spec.action.stat = "max"
        elif "最小" in question or "最低" in question:
            spec.action.stat = "min"
        elif "合计" in question or "总计" in question or "总数" in question:
            spec.action.stat = "sum"
        elif "去重" in question:
            spec.action.stat = "distinct"
    # 对比目标
    if spec.intent == "compare":
        if "同比" in question or "年" in question and ("去年同期" in question or "同比" in question):
            spec.action.compare_target = "yoy"
        elif "环比" in question or "上个月" in question or "上月" in question or "上期" in question:
            spec.action.compare_target = "mom"
        elif "vs" in question.lower() or "对比" in question or "比较" in question:
            spec.action.compare_target = "dim"
    # 置信度
    conf = 0.45
    conf += 0.15 * len(spec.metrics)
    conf += 0.08 * len(spec.dimensions)
    conf += 0.08 * len(spec.filters)
    if time_matched:
        conf += 0.1
    if spec.intent != "value":
        conf += 0.1
    spec.confidence = min(round(conf, 2), 0.95)
    # 缺失要素
    missing = []
    if not spec.metrics:
        missing.append("指标")
    if not spec.time.start and spec.intent != "detail":
        missing.append("时间")
    spec.missing = missing
    return spec


def _merge_clarify_answer(spec: QuerySpec, answer: dict | None,
                          candidates: dict, dicts: dict[str, list[dict]]) -> None:
    """合并澄清点选结果：answer 形如 {"metrics": ["销售额"], "dimensions": [], "time": "本月"}。"""
    if not answer:
        return
    for m in (answer.get("metrics") or []):
        if isinstance(m, str) and m and not any(x.name == m or m in x.name for x in spec.metrics):
            hit = None
            for cm in candidates["metrics"]:
                if (cm.get("comment") or "") == m or cm.get("column") == m:
                    hit = cm
                    break
            if hit is None:
                for cm in candidates["metrics"]:
                    if m in (cm.get("comment") or "") or m in (cm.get("column") or ""):
                        hit = cm
                        break
            if hit:
                spec.metrics.append(MetricSpec(name=hit.get("comment") or m,
                                               alias=hit.get("column", ""),
                                               agg=hit.get("agg", "sum"), source="clarify"))
    for d in (answer.get("dimensions") or []):
        if isinstance(d, str) and d and not any(x.name == d for x in spec.dimensions):
            hit = None
            for cd in candidates["dimensions"]:
                if (cd.get("comment") or "") == d or cd.get("column") == d:
                    hit = cd
                    break
            if hit:
                spec.dimensions.append(DimensionSpec(name=hit.get("comment") or d,
                                                     alias=hit.get("column", ""),
                                                     source="clarify"))
    expr = (answer.get("time") or "").strip()
    if expr:
        ts = parse_time_expr(expr)
        if ts:
            spec.time = ts
    top_n = answer.get("top_n")
    if isinstance(top_n, int):
        spec.action.top_n = top_n
    if spec.metrics:
        spec.confidence = min(spec.confidence + 0.3, 0.95)
        spec.missing = [x for x in spec.missing if x != "指标"]


def _resolve_schema(datasource_id: int, question: str,
                    schema_name: str | None = None) -> str | None:
    """解析查询范围 schema（R1/R2/R3）：
    1. 前端显式指定 schema_name（优先，须在已采集清单中）；
    2. 问题中提取「项目/库」标识（如 RT项目 → RT），精确匹配 schema 清单；
    3. 单 schema 数据源 → 直接使用默认 schema；
    4. 均无法确定 → None（不限定）。
    """
    from .mapping import fetch_schemas
    schemas = fetch_schemas(datasource_id)
    if not schemas:
        return None
    if schema_name and schema_name.strip():
        sn = schema_name.strip()
        if sn in schemas:
            return sn
        logger.warning("前端指定 schema 不在已采集清单: %s", sn)
    cand = extract_schema_expr(question)
    if cand:
        exact = [s for s in schemas if s.lower() == cand.lower()]
        if exact:
            return exact[0]
        # 模糊匹配仅限长度 ≥3 的标识，避免 "RT" 命中 "AITS_hub" 这类巧合子串
        if len(cand) >= 3:
            fuzzy = [s for s in schemas
                     if cand.lower() in s.lower() or s.lower() in cand.lower()]
            if len(fuzzy) == 1:
                return fuzzy[0]
    if len(schemas) == 1:
        return schemas[0]
    return None


def parse_query_spec(question: str, datasource_id: int, workspace_id: int,
                     llm=None, prev_spec: QuerySpec | None = None,
                     clarify_answer: dict | None = None,
                     dicts: dict[str, list[dict]] | None = None,
                     templates: list[dict] | None = None,
                     require_confident: float = 0.6,
                     schema_name: str | None = None,
                     confirmed_tables: list[str] | None = None) -> dict:
    """意图理解层入口。

    返回：
    {"status": "ok", "spec": QuerySpec} |
    {"status": "clarify", "message": "...", "candidates": [...], "missing": [...]} |
    {"status": "refuse", "message": "..."}

    confirmed_tables: 用户已确认的查询表（表澄清勾选后回填轮传入）。
    非空时，规则引擎与 LLM 抽取指标/维度/时间只允许从确认表字段中选择，
    避免 Spec 展示与实际查询表脱节（如查"会议"却展示审批表的授权类型字段）。
    """
    from ..database import SessionLocal
    schema = _resolve_schema(datasource_id, question, schema_name)
    candidates = fetch_schema_candidates(datasource_id, schema, confirmed_tables)
    if not candidates["metrics"] and not candidates["dimensions"]:
        return {"status": "refuse",
                "message": "当前数据源尚未采集到可查询的表字段（Schema），请先在「数据源」中同步 Schema。"}

    # 问题重构（仅首轮、未点选澄清时，LLM 可用才执行）：
    # 消歧 / 吸收负向澄清 / 提炼业务实体与派生维度 → 规范化问数描述，
    # 后续规则引擎与 LLM 解析均基于重构后文本，提升 QuerySpec 准确率与 SQL 生成可行性。
    raw_question = question
    rewritten: dict | None = None
    if (prev_spec is None and clarify_answer is None
            and llm is not None and llm.configured):
        rewritten = rewrite_question(question, llm)
        if rewritten:
            logger.info("[问数][rewrite] 问题重构: %r → %r | intent_hint=%s metric_hint=%s "
                        "dimension_hint=%s negatives=%s",
                        raw_question[:60], str(rewritten.get("question"))[:80],
                        rewritten.get("intent_hint"), rewritten.get("metric_hint"),
                        rewritten.get("dimension_hint"), rewritten.get("negatives"))
    if rewritten and str(rewritten.get("question") or "").strip():
        question = str(rewritten["question"]).strip()
    else:
        logger.info("[问数][rewrite] 未重构（无LLM/多轮追问/澄清点选/模型未改写），沿用原问题")

    # LLM 优先（L-A 层）：意图/spec 抽取交 LLM，规则为兜底
    _guidance_source = "rule_fallback"
    is_detail_pass = False
    if (llm is not None and llm.configured and not clarify_answer):
        data = _llm_extract_spec(question, datasource_id, llm, prev_spec, schema,
                                 rewrite_hint=rewritten,
                                 confirmed_tables=confirmed_tables)
        if data:
            llm_spec = _spec_from_llm_data(data, question, prev_spec)
            if llm_spec and llm_spec.metrics:
                spec = llm_spec
                spec.schema_name = schema or ""
                _guidance_source = "llm"
                if spec.time.start == "" or spec.time.expr == "近30天（默认）":
                    _extract_time(question, spec)
                logger.info("[问数][spec] LLM 主导解析: intent=%s metrics=%s dims=%s | source=%s",
                            spec.intent, [(m.name, m.agg) for m in spec.metrics],
                            [d.name for d in spec.dimensions], _guidance_source)
            else:
                logger.warning("[问数][spec] LLM 返回空/无效，回退规则引擎")
                spec = _rule_extract_spec(question, candidates, dicts or {})
                spec.schema_name = schema or ""
        else:
            spec = _rule_extract_spec(question, candidates, dicts or {})
            spec.schema_name = schema or ""
    else:
        # 无 LLM 或澄清轮次 → 规则引擎（L-C 兜底）
        spec = _rule_extract_spec(question, candidates, dicts or {})
        spec.schema_name = schema or ""

    # 多轮继承（规则路径）：省略式追问
    if prev_spec and not spec.metrics and spec.intent == "value":
        inherited_q = question
        if "环比" in inherited_q or "同比" in inherited_q:
            spec.intent = "compare"
            spec.action.compare_target = "yoy" if "同比" in inherited_q else "mom"
        spec.metrics = prev_spec.metrics
        spec.dimensions = prev_spec.dimensions
        spec.inherited_from = {"metrics": True, "dimensions": True}
        if "去年" in inherited_q or "同期" in inherited_q:
            spec.action.compare_target = "yoy"
        spec.confidence = min(spec.confidence + 0.2, 0.95)

    # 澄清点选合并
    if clarify_answer:
        _merge_clarify_answer(spec, clarify_answer, candidates, dicts or {})

    is_detail_pass = spec.intent == "detail" and not spec.metrics

    # 统一后处理：分组统计纠正（规则/LLM 路径均生效）
    _group_kw = ("每个", "各个", "分别", "各项目", "各渠道", "各地区", "各部门", "各类型", "各状态")
    if spec.intent == "value" and any(k in question for k in _group_kw):
        spec.intent = "statistic"
        if not spec.action.stat:
            spec.action.stat = "sum"
        if not spec.dimensions:
            for _dim_name in ("项目", "渠道", "地区", "部门", "类型", "状态", "产品", "用户"):
                if _dim_name in question:
                    spec.dimensions = [DimensionSpec(name=_dim_name, source="rule")]
                    break

    # 溯源：原始问题 + 问题重构后的规范化描述
    spec.original_question = raw_question
    if rewritten:
        spec.rewritten_question = question
        spec.table_hints = [h for h in (rewritten.get("table_hints") or []) if h]
        if spec.table_hints:
            logger.info("[问数][spec] 拆表检索词 %s 注入 spec", spec.table_hints)

    return {"status": "ok", "spec": spec, "guidance_source": _guidance_source}
