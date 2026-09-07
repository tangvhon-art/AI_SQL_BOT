"""L1 预处理层：口语化清洗、业务词纠错、冗余去除、句式标准化、指代消解。

原则：只做低风险变换——无法确定语义的改写直接原样透传，宁可不改不可错改。

词表（filler_prefix / deixis_signal）统一从 biz_lexicon 获取，不在本模块硬编码。
"""
from __future__ import annotations

import re
from typing import Any

from .biz_lexicon import get as lex_get

_QUOTE_MAP = str.maketrans({
    "\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'",
    "\uff1a": ":", "\uff0c": ",",
})


def clean_text(text: str) -> str:
    """清洗：去首尾空白、统一标点、去请求前缀、压缩空白。"""
    t = (text or "").strip()
    if not t:
        return t
    t = t.translate(_QUOTE_MAP)
    lowered = t.lower()
    for p in lex_get("filler_prefix"):
        if lowered.startswith(p):
            t = t[len(p):].lstrip(" 的")
            lowered = t.lower()
    t = re.sub(r"\s+", " ", t).strip(" ，。？！!?；;、")
    return t


# 业务词常见错别字/拼音（本模块唯一硬编码 dict，仅此一处使用）
_TYPO_MAP = {
    "定单": "订单", "定单量": "订单量", "销受": "销售", "xiaoshoue": "销售额",
    "shouru": "收入", "lirun": "利润", "chengben": "成本", "yonghu": "用户",
    "huoli": "活跃", "furong": "复购", "tuikuan": "退款", "zhifu": "支付",
    "shangpin": "商品", "qudao": "渠道", "diqu": "地区", "chengshi": "城市",
}


def correct_biz_words(text: str, dict_terms: list[str] | None = None) -> str:
    """业务词纠错：错别字表 + 业务词典近似匹配（编辑距离≤1）。"""
    t = text
    for wrong, right in _TYPO_MAP.items():
        if wrong in t.lower():
            t = re.sub(re.escape(wrong), right, t, flags=re.IGNORECASE)
    if dict_terms:
        words = re.findall(r"[\u4e00-\u9fa5]{2,}", t)
        for w in words:
            for term in dict_terms:
                if w != term and len(w) >= 2 and _edit_distance(w, term) == 1:
                    t = t.replace(w, term)
                    break
    return t


def _edit_distance(a: str, b: str) -> int:
    if abs(len(a) - len(b)) > 1:
        return 9
    if a == b:
        return 0
    if len(a) == len(b):
        return sum(1 for x, y in zip(a, b) if x != y)
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    for i in range(len(long_)):
        if long_[:i] + long_[i + 1:] == short:
            return 1
    return 9


def is_deixis_turn(question: str, has_prev: bool) -> bool:
    """判断当前轮是否为省略式追问（依赖上一轮 QuerySpec）。"""
    if not has_prev or not question:
        return False
    q = question.strip()
    if len(q) > 24:
        return False
    return any(sig in q for sig in lex_get("deixis_signal"))


def normalize_question(question: str, prev_spec: dict | None = None,
                       dict_terms: list[str] | None = None) -> tuple[str, dict[str, Any]]:
    """预处理入口：返回 (标准问题, 继承标注)。

    prev_spec: 上一轮 QuerySpec dict；命中省略式追问时返回 inherited_from 标注，
    由意图层负责具体继承逻辑（指标/维度继承、时间替换）。
    """
    t = clean_text(question)
    t = correct_biz_words(t, dict_terms)
    inherited: dict[str, Any] = {}
    if prev_spec and is_deixis_turn(t, True):
        inherited = {
            "has_prev": True,
            "prev_metrics": [m.get("name", "") for m in prev_spec.get("metrics", [])],
            "prev_dimensions": [d.get("name", "") for d in prev_spec.get("dimensions", [])],
            "prev_time": prev_spec.get("time", {}),
            "prev_intent": prev_spec.get("intent", ""),
        }
    return t, inherited
