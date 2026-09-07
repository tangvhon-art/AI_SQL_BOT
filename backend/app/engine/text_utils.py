"""L-0 公共化地基层：统一文本分词 / 匹配 / 打分。

所有上层模块（nl2sql / rag / mapping / intent / chart）必须调用此模块，
不允许各自重复实现分词或文本匹配逻辑。
"""
from __future__ import annotations

import re


def tokenize(text: str) -> set[str]:
    """统一中文分词：CJK 连续串生成 2/3-gram + 拉丁/数字/下划线原词。

    替代 nl2sql._tokenize_cn（2/3/4-gram）、rag._kw_set（2-gram）、
    mapping._contains_term 内部分词（2/3-gram）三套不一致实现。

    统一策略：CJK 2/3-gram（3-gram 覆盖三字词如"销售额"，2-gram 覆盖二字词），
    拉丁/数字/下划线按原词（≥2 字符）。
    """
    if not text:
        return set()
    q = text.lower()
    tokens: set[str] = set()
    for run in re.findall(r"[\u4e00-\u9fa5]+", q):
        L = len(run)
        for n in (2, 3):
            for i in range(L - n + 1):
                tokens.add(run[i:i + n])
    tokens.update(w for w in re.findall(r"[a-z0-9_]+", q) if len(w) >= 2)
    return tokens


def kw_set(text: str) -> set[str]:
    """向后兼容别名：tokenize 的旧名，rag.py 无需改调用方。"""
    return tokenize(text)


def contains_term(text: str, term: str) -> bool:
    """统一文本包含判断：子串 + 中文 n-gram 部分命中。

    替代 mapping._contains_term。
    判断逻辑：term ≥2 字符时，先做子串匹配，再做中文 2/3-gram 部分命中。
    """
    if not term or not text:
        return False
    t = text.lower()
    term_l = term.lower()
    if len(term_l) >= 2:
        if term_l in t:
            return True
        if re.search(r"[\u4e00-\u9fa5]", term_l) and re.search(r"[\u4e00-\u9fa5]", t):
            for ln in (3, 2):
                segs = {t[i:i + ln] for i in range(max(1, len(t) - ln + 1))}
                if any(s and s in term_l for s in segs):
                    return True
    return False


def match_score(text: str, term: str) -> float:
    """统一匹配打分：完全匹配 +10 / 注释命中 +6 / 字段名命中 +4 / 短词降权 -2。

    替代 nl2sql.retrieve_candidate_tables 打分与 mapping._pick_best 两套权重。
    text 为待匹配文本（注释+字段名拼接），term 为搜索词。
    """
    if not text or not term:
        return 0.0
    t = text.lower()
    term_l = term.lower()
    score = 0.0
    if term_l and (term_l in t or t in term_l):
        score += 10
    if term_l in t:
        score += 6
    if term_l and re.search(rf"(?<![a-z0-9_]){re.escape(term_l)}(?![a-z0-9_])", t):
        score += 4
    if score > 0 and len(term_l) <= 2:
        score -= 2
    return score
