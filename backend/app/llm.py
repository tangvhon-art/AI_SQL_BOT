"""LLM 客户端：OpenAI 兼容统一抽象；未配置 Key 时进入 mock 演示模式。"""
import json
import logging

import httpx

from .config import get_settings

logger = logging.getLogger(__name__)


class LLMError(Exception):
    pass


class LLMClient:
    """OpenAI 兼容 chat/embedding 客户端。"""

    def __init__(self, base_url: str = "", api_key: str = "", model: str = "",
                 embedding_model: str = "", temperature: float = 0.1, max_tokens: int = 2048):
        s = get_settings()
        self.base_url = (base_url or s.llm_base_url).rstrip("/")
        self.api_key = api_key or s.llm_api_key
        self.model = model or s.llm_model
        self.embed_model = embedding_model or s.embed_model
        self.temperature = temperature
        self.max_tokens = max_tokens

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def chat(self, messages: list[dict], temperature: float | None = None,
             max_tokens: int | None = None, json_mode: bool = False,
             thinking: bool = True) -> str:
        """调用 chat/completions；返回文本内容。未配置时抛 LLMError 由上层降级 mock。
        thinking=True 时对推理模型开启 enable_thinking：reasoning_content 为思考过程、
        content 为最终回答（content 为空时兜底取 reasoning，避免 JSON 抽取任务丢失结果）。"""
        if not self.configured:
            raise LLMError("LLM 未配置")
        # 部分推理模型不支持 response_format 或返回空 content：先 json_mode，空则降级为普通模式重试
        for attempt, use_json in enumerate([json_mode, False]):
            payload = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature if temperature is not None else self.temperature,
                "max_tokens": max_tokens or self.max_tokens,
            }
            if use_json:
                payload["response_format"] = {"type": "json_object"}
            payload["enable_thinking"] = thinking
            with httpx.Client(timeout=120) as client:
                resp = client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
                if resp.status_code != 200:
                    raise LLMError(f"LLM 调用失败 {resp.status_code}: {resp.text[:300]}")
                data = resp.json()
            content = (data["choices"][0]["message"].get("content") or "").strip()
            if not content:
                # 推理模型可能把内容放在 reasoning_content（思考过程兜底）
                rc = (data["choices"][0]["message"].get("reasoning_content") or "").strip()
                if rc:
                    content = rc
            if content:
                return content
            if attempt == 0 and use_json:
                continue  # json_mode 返回空 → 降级普通模式
            raise LLMError(f"LLM 返回空内容: {str(data)[:300]}")

    def chat_stream(self, messages: list[dict], temperature: float | None = None,
                    max_tokens: int | None = None, json_mode: bool = False,
                    thinking: bool = True) -> dict:
        """流式调用 chat/completions；逐块 yield {"content": "...", "reasoning": "..."}。
        正确的推理模型流式语义：thinking=True 时开启 enable_thinking，
        reasoning_content（思考过程）与 content（最终回答）严格分离、互不合并；
        生成器结束后返回 {"content": full_content, "reasoning": full_reasoning}。"""
        if not self.configured:
            raise LLMError("LLM 未配置")
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.temperature,
            "max_tokens": max_tokens or self.max_tokens,
            "stream": True,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        payload["enable_thinking"] = thinking
        full_content = ""
        full_reasoning = ""
        with httpx.Client(timeout=120) as client:
            with client.stream("POST", f"{self.base_url}/chat/completions",
                               headers={"Authorization": f"Bearer {self.api_key}"},
                               json=payload) as resp:
                if resp.status_code != 200:
                    raise LLMError(f"LLM 流式调用失败 {resp.status_code}: {resp.text[:300]}")
                for line in resp.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                        delta = obj["choices"][0]["delta"]
                        content_delta = delta.get("content") or ""
                        reasoning_delta = delta.get("reasoning_content") or ""
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
                    if content_delta:
                        full_content += content_delta
                    if reasoning_delta:
                        full_reasoning += reasoning_delta
                    if content_delta or reasoning_delta:
                        yield {"content": content_delta, "reasoning": reasoning_delta}
        return {"content": full_content.strip(), "reasoning": full_reasoning.strip()}

    def embed(self, texts: list[str], model: str = "") -> list[list[float]]:
        if not self.configured:
            raise LLMError("LLM 未配置")
        with httpx.Client(timeout=60) as client:
            resp = client.post(
                f"{self.base_url}/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": model or self.embed_model, "input": texts},
            )
            if resp.status_code != 200:
                raise LLMError(f"Embedding 失败 {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
        return [item["embedding"] for item in data["data"]]


def parse_json_content(content: str) -> dict:
    """LLM 输出 JSON 解析（容忍 markdown 代码围栏、前后多余文本、重复 JSON）。"""
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 逐段提取第一段完整的平衡 JSON（容忍推理模型输出多余文本/重复 JSON）
    decoder = json.JSONDecoder()
    idx = 0
    while True:
        start = text.find("{", idx)
        if start < 0:
            break
        try:
            obj, _ = decoder.raw_decode(text[start:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            idx = start + 1
    raise LLMError(f"LLM 输出非 JSON: {content[:200]}")
