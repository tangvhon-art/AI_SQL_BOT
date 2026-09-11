"""L-0 公共化地基层：LLM JSON 输出解析 / 确定性修复 / 带诊断的重试。

统一 LLM JSON 调用：发送 prompt → 提取 JSON（含瑕疵修复）→ 失败带诊断重试 N 次
→ 返回 dict 或 None（供上层降级）。

公共契约（向后兼容，禁止破坏）：
- extract_json(content) -> dict | None：解析失败返回 None；裸 JSON 数组统一包装为
  {"tables": [...]}（选表场景依赖此形态）；
- ask(llm, system, user, max_tokens, temperature, max_retries) -> dict | None：
  llm 为 None/未配置直接返回 None；调用 llm.chat(messages, max_tokens, temperature, thinking=False)。
"""
from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

# ```json ... ``` / ``` ... ``` 代码块围栏
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
# 尾逗号：} 或 ] 之前的多余逗号
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
# Python 字面量风格单引号：key 与 value
_SINGLE_QUOTE_KEY_RE = re.compile(r"([{,]\s*)'([^']*?)'(\s*:)")
_SINGLE_QUOTE_VAL_RE = re.compile(r"(:\s*)'([^']*?)'(\s*[,}\]])")
# BOM / 零宽字符
_INVISIBLE_RE = re.compile(r"[\ufeff\u200b\u200d\u200c]")


def _strip_fence(blob: str) -> str:
    m = _FENCE_RE.search(blob)
    return m.group(1).strip() if m else blob


def repair_json_text(text: str) -> str:
    """对 LLM 常见 JSON 瑕疵做确定性修复，返回修复后的字符串（不保证一定可解析）。

    覆盖：代码围栏/前后缀切片、BOM/零宽字符、单引号→双引号、尾逗号。
    """
    if not text:
        return text
    t = text.strip()
    t = _strip_fence(t)
    t = _INVISIBLE_RE.sub("", t)
    # 单引号 → 双引号（key/value 各两轮，覆盖嵌套）
    for _ in range(2):
        t = _SINGLE_QUOTE_KEY_RE.sub(lambda m: f'{m.group(1)}"{m.group(2)}"{m.group(3)}', t)
        t = _SINGLE_QUOTE_VAL_RE.sub(lambda m: f'{m.group(1)}"{m.group(2)}"{m.group(3)}', t)
    # 去尾逗号（循环到稳定）
    prev = None
    while prev != t:
        prev = t
        t = _TRAILING_COMMA_RE.sub(r"\1", t)
    return t.strip()


def _loads_one(blob: str):
    """单次 json.loads，成功返回 dict/list，失败返回 None。"""
    try:
        return json.loads(blob)
    except (json.JSONDecodeError, ValueError):
        return None


def extract_json(content: str) -> dict | None:
    """从 LLM 回答中提取 JSON 对象，返回 dict；失败返回 None。

    解析顺序：原文 → 去围栏 → 切片 {..}/[..] → 确定性修复后再解析。
    裸 JSON 数组统一包装为 {"tables": [...]}（选表场景依赖）。
    """
    if not content or not str(content).strip():
        return None
    raw = str(content).strip()

    # 候选文本序列：原文 → 去围栏 → 修复文本
    candidates = [raw]
    unfenced = _strip_fence(raw)
    if unfenced != raw:
        candidates.append(unfenced)
    repaired = repair_json_text(raw)
    if repaired not in candidates:
        candidates.append(repaired)

    for blob in candidates:
        parsed = _loads_one(blob)
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {"tables": parsed}

    # 切片最外层 { ... } / [ ... ] 再试（混杂前后缀文字的情况）
    for blob in list(candidates):
        s_obj, e_obj = blob.find("{"), blob.rfind("}")
        if s_obj != -1 and e_obj > s_obj:
            parsed = _loads_one(blob[s_obj:e_obj + 1])
            if isinstance(parsed, dict):
                return parsed
            if isinstance(parsed, list):
                return {"tables": parsed}
            # 切片后仍失败，对切片再做一次修复
            parsed = _loads_one(repair_json_text(blob[s_obj:e_obj + 1]))
            if isinstance(parsed, dict):
                return parsed
            if isinstance(parsed, list):
                return {"tables": parsed}
        s_arr, e_arr = blob.find("["), blob.rfind("]")
        if s_arr != -1 and e_arr > s_arr:
            parsed = _loads_one(blob[s_arr:e_arr + 1])
            if isinstance(parsed, list):
                return {"tables": parsed}
            if isinstance(parsed, dict):
                return parsed
    return None


def _diagnose(raw: str) -> str:
    """根据上一轮原文生成具体修正指令（带诊断的重试，避免空转）。"""
    text = (raw or "").strip()
    if not text:
        return "上一次返回为空，请只输出 JSON 本体，不要输出思考过程或说明文字"
    if "```" in text:
        return ("上一次输出包含 Markdown 代码围栏或多余文字，请去掉 ```json/``` 标记与说明，"
                "第一个字符直接是 { 或 [，最后一个字符是 } 或 ]")
    if re.search(r",\s*[}\]]", text):
        return "上一次 JSON 在 } 或 ] 前有多余尾逗号，请删除所有尾逗号"
    if "'" in text and '"' not in text:
        return "上一次使用了单引号，JSON 必须用双引号包裹键名与字符串值"
    if "{" not in text and "[" not in text:
        return "上一次输出完全是自然语言、没有 JSON 结构，请只输出约定的 JSON"
    return ("上一次输出不是合法 JSON，请核对括号/引号是否成对、键名是否用双引号、"
            "对象与数组是否闭合，只输出一个合法 JSON")


def ask(llm, system: str, user: str,
        max_tokens: int = 1600,
        temperature: float = 0.1,
        max_retries: int = 1) -> dict | None:
    """统一 LLM JSON 调用。

    流程：发送 prompt → 提取 JSON（含瑕疵修复）→ 失败带诊断重试 max_retries 次
    → 返回 dict 或 None（供上层降级）。llm 为 None 或未配置时返回 None。
    """
    if llm is None or not getattr(llm, "configured", False):
        return None

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    raw = ""
    for attempt in range(max_retries + 1):
        try:
            raw = llm.chat(messages, max_tokens=max_tokens,
                           temperature=temperature, thinking=False)
            data = extract_json(raw)
            if data is not None:
                return data
            logger.warning("[llm_json] 第 %d 次尝试解析失败，raw: %r",
                           attempt + 1, (raw or "")[:200])
        except Exception as exc:  # noqa: BLE001
            logger.warning("[llm_json] 第 %d 次调用失败: %s", attempt + 1, exc)
        if attempt < max_retries:
            # 带诊断的重试：附上一轮原文 + 具体失败原因，让重试有增量信息
            messages.append({"role": "assistant", "content": (raw or "")[:800]})
            messages.append({"role": "user", "content": (
                f"【输出修正】{_diagnose(raw)}。请重新输出完整合法的 JSON，"
                "字段与键名保持系统提示词要求不变，不要输出多余文字。")})
    return None
