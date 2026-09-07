"""L-0 公共化地基层：LLM JSON 输出解析 / Schema 校验 / 重试策略。

统一 LLM JSON 调用：发送 prompt → 提取 JSON → Schema 校验 → 失败重试 N 次 → 返回 dict 或 None。

替代 intent._llm_extract_spec / nl2sql._parse_select_blob / chart 各自的 JSON 解析+容错+重试。
"""
from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)


def extract_json(content: str) -> dict | None:
    """从 LLM 回答中提取 JSON。

    支持：
    1. ```json ... ``` 代码块
    2. 裸 JSON 对象 { ... }
    3. 裸 JSON 数组 [ ... ]
    4. 混杂文本中的 JSON 片段

    迁自 nl2sql._parse_select_blob / intent._spec_from_llm_data 的 JSON 提取逻辑。
    """
    if not content or not content.strip():
        return None
    blob = content.strip()

    # 1) 先尝试从 ```json ... ``` 代码块提取
    m = re.search(r"```(?:json)?\s*(.*?)```", blob, re.S)
    if m:
        blob = m.group(1).strip()

    # 2) 尝试解析完整 JSON
    try:
        parsed = json.loads(blob)
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {"tables": parsed}
    except (json.JSONDecodeError, ValueError):
        pass

    # 3) 尝试提取 { ... } 对象
    s, e = blob.find("{"), blob.rfind("}")
    if s != -1 and e > s:
        try:
            obj = json.loads(blob[s:e + 1])
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            pass

    # 4) 尝试提取 [ ... ] 数组（选表场景）
    s, e = blob.find("["), blob.rfind("]")
    if s != -1 and e > s:
        try:
            arr = json.loads(blob[s:e + 1])
            if isinstance(arr, list):
                # 包装为 dict 供统一处理
                return {"tables": arr}
        except (json.JSONDecodeError, ValueError):
            pass

    # 5) 兼容 {"tables": [...]} / {"selected": [...]}
    s, e = blob.find("{"), blob.rfind("}")
    if s != -1 and e > s:
        try:
            obj = json.loads(blob[s:e + 1])
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            pass

    return None


def ask(llm, system: str, user: str,
        max_tokens: int = 1600,
        temperature: float = 0.1,
        max_retries: int = 1) -> dict | None:
    """统一 LLM JSON 调用。

    流程：发送 prompt → 提取 JSON → 返回 dict 或 None（供上层降级）。
    失败重试 max_retries 次。llm 为 None 或未配置时返回 None。

    替代 intent._llm_extract_spec / nl2sql._llm_select_tables 各自的 JSON 解析+容错+重试。
    """
    if llm is None or not getattr(llm, "configured", False):
        return None

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

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
                messages.append({"role": "assistant", "content": raw if "raw" in dir() else ""})
                messages.append({"role": "user", "content": "请严格输出 JSON 格式，不要输出多余文字。"})
    return None
