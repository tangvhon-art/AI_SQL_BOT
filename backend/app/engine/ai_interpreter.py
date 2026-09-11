"""AI 解读引擎（公共层）。

基于 Dashboard 数据 + 公共 Prompt 模板，生成结构化解读结果。
支持流式输出（SSE 推送），结果可保存为报告。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Generator

from ..models import Prompt
from .prompt_service import PromptService
from .llm_json import extract_json

logger = logging.getLogger(__name__)


class InterpretationResult:
    """结构化解读结果。"""

    def __init__(self, summary: str = "", key_metrics: list | None = None,
                 trends: list | None = None, comparisons: list | None = None,
                 anomalies: list | None = None, suggestions: list | None = None,
                 raw_text: str = ""):
        self.summary = summary
        self.key_metrics = key_metrics or []
        self.trends = trends or []
        self.comparisons = comparisons or []
        self.anomalies = anomalies or []
        self.suggestions = suggestions or []
        self.raw_text = raw_text
        self.parse_ok = False  # 是否成功解析为结构化 JSON（用于决定是否带诊断重试）

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "key_metrics": self.key_metrics,
            "trends": self.trends,
            "comparisons": self.comparisons,
            "anomalies": self.anomalies,
            "suggestions": self.suggestions,
            "raw_text": self.raw_text,
        }


class AiInterpreter:
    """AI 解读引擎。

    用法：
        interpreter = AiInterpreter(llm=llm_client, prompt_service=prompt_svc)
        for event in interpreter.interpret_stream(dashboard_data, prompt_template_id=123):
            yield event  # SSE 事件
    """

    def __init__(self, llm: Any | None = None, prompt_service: PromptService | None = None):
        self.llm = llm
        self.prompt_service = prompt_service

    def interpret_stream(
        self,
        question: str,
        dashboard_data: dict,
        prompt_template_id: int | None = None,
        scene_type: str = "ai_interpret",
    ) -> Generator[dict, None, None]:
        """流式解读：yield SSE 事件字典。

        事件类型：
        - ai_interpretation_start: 开始解读
        - ai_interpretation: 流式增量（delta 文本）
        - ai_interpretation_done: 完成，携带结构化结果
        - ai_interpretation_error: 错误
        """
        if not self.llm or not self.llm.configured:
            yield {"type": "ai_interpretation_error", "error": "LLM 未配置"}
            return

        # 1. 获取 Prompt 模板
        template: Prompt | None = None
        if self.prompt_service and prompt_template_id:
            template = self.prompt_service.get(prompt_template_id)
        if not template and self.prompt_service:
            template = self.prompt_service.get_default(scene_type)

        template_text = template.prompt_template if template else self._default_template()
        template_name = template.name if template else "默认模板"

        # 2. 准备变量
        data_summary = self._build_data_summary(dashboard_data)
        metrics = self._extract_metrics(dashboard_data)
        variables = {
            "question": question,
            "data_summary": data_summary,
            "metrics": json.dumps(metrics, ensure_ascii=False, default=str),
        }

        # 3. 渲染 Prompt
        prompt = PromptService.render(template_text, variables)

        yield {"type": "ai_interpretation_start", "template_name": template_name}

        # 4. 调用 LLM（流式）：提示词作为 system prompt，原始问题+数据作为 user 消息
        # （部分 LLM 网关要求消息列表必须含 user 消息）
        full_text = ""
        try:
            for delta in self._llm_stream(
                    prompt,
                    question=question,
                    data_summary=data_summary,
                    metrics_json=json.dumps(metrics, ensure_ascii=False, default=str)):
                full_text += delta
                yield {"type": "ai_interpretation", "delta": delta, "raw_text": full_text}
        except Exception as exc:  # noqa: BLE001
            logger.warning("AI 解读 LLM 调用失败: %s", exc)
            yield {"type": "ai_interpretation_error", "error": str(exc)}
            return

        # 5. 解析结构化结果；解析失败（未得到合法 JSON）→ 带诊断重试 1 次，仍失败才全文兜底
        result = self._parse_result(full_text)
        if not result.parse_ok:
            retry_text = self._retry_structured(
                prompt, question, data_summary,
                json.dumps(metrics, ensure_ascii=False, default=str), full_text)
            if retry_text:
                retry_result = self._parse_result(retry_text)
                if retry_result.parse_ok:
                    logger.info("AI 解读首次解析失败，带诊断重试后成功")
                    full_text, result = retry_text, retry_result
        result.raw_text = full_text
        yield {"type": "ai_interpretation_done", "result": result.to_dict(), "raw_text": full_text}

    def interpret(self, question: str, dashboard_data: dict,
                  prompt_template_id: int | None = None) -> InterpretationResult:
        """非流式解读：返回结构化结果。"""
        result = InterpretationResult()
        for event in self.interpret_stream(question, dashboard_data, prompt_template_id):
            if event["type"] == "ai_interpretation_done":
                return InterpretationResult(**event["result"])
            if event["type"] == "ai_interpretation_error":
                result.raw_text = f"解读失败: {event.get('error', '')}"
        return result

    # ---------- 内部方法 ----------

    def _llm_stream(self, prompt: str, question: str = "",
                    data_summary: str = "", metrics_json: str = "") -> Generator[str, None, None]:
        """调用 LLM 流式接口，yield 增量文本。

        消息结构：提示词模板作为 system prompt 下发（「用所选提示词作为 system prompt 解读」），
        原始问题 + 数据摘要 + 关键指标作为 user 消息（部分 LLM 网关要求消息列表必须含 user 消息）。
        """
        user_parts = []
        if question:
            user_parts.append(f"原始问题：{question}")
        if data_summary:
            user_parts.append(f"数据摘要：\n{data_summary}")
        if metrics_json and metrics_json.strip() not in ("", "[]"):
            user_parts.append(f"关键指标：{metrics_json}")
        user_content = "\n".join(user_parts) if user_parts else "请基于以上数据进行分析并输出结果。"
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_content},
        ]
        try:
            for chunk in self.llm.chat_stream(messages, temperature=0.3, thinking=False):
                delta = chunk.get("content") or ""
                if delta:
                    yield delta
        except Exception:
            # 非流式回退
            resp = self.llm.chat(messages, temperature=0.3, thinking=False)
            text = resp if isinstance(resp, str) else (resp.get("content") if isinstance(resp, dict) else str(resp))
            yield text

    @staticmethod
    def _default_template() -> str:
        return (
            "你是一位严谨的资深数据分析师，只基于提供的数据作答。\n"
            "原始问题：{{question}}\n"
            "数据摘要：{{data_summary}}\n"
            "关键指标：{{metrics}}\n\n"
            "【输出协议（必须严格遵守）】\n"
            "1. 只输出一个合法 JSON 对象，禁止 Markdown 代码围栏、解释文字、推理过程；第一个字符必须是 {，最后一个字符必须是 }；\n"
            "2. 键名固定为 summary/key_metrics/trends/comparisons/anomalies/suggestions，不得增删改名；\n"
            "3. summary 为字符串总体结论；key_metrics 为数组，元素为 {name,value,change}；"
            "trends/comparisons/suggestions 为字符串数组；anomalies 元素为 {desc,severity}，severity 只能取 high/medium/low；\n"
            "4. 所有数字、名称必须来自上方数据，禁止编造数据中不存在的数值；无内容的字段输出空数组 [] 或空字符串，不要输出 null；\n"
            "5. 即使数据不足，也必须输出结构完整的最小合法 JSON（空数组占位），不得输出自然语言。\n"
            "结构模板：\n"
            '{"summary": "总体结论", "key_metrics": [{"name":"","value":"","change":""}], '
            '"trends": [], "comparisons": [], "anomalies": [{"desc":"","severity":"high"}], '
            '"suggestions": []}'
        )

    def _retry_structured(self, prompt: str, question: str, data_summary: str,
                          metrics_json: str, bad_text: str) -> str:
        """首次输出无法解析为结构化 JSON 时，带诊断重试 1 次（非流式），失败返回空串。"""
        try:
            user_parts = []
            if question:
                user_parts.append(f"原始问题：{question}")
            if data_summary:
                user_parts.append(f"数据摘要：\n{data_summary}")
            if metrics_json and metrics_json.strip() not in ("", "[]"):
                user_parts.append(f"关键指标：{metrics_json}")
            messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": "\n".join(user_parts)},
                {"role": "assistant", "content": (bad_text or "")[:800]},
                {"role": "user", "content": (
                    "【输出修正】你上一次的回答无法解析为约定的 JSON。请去掉 Markdown 围栏与多余文字，"
                    "只输出一个键名完整、括号引号成对的合法 JSON 对象；无内容字段用空数组/空字符串占位，"
                    "不要输出 null，不要重复上一次的错误格式。")},
            ]
            resp = self.llm.chat(messages, temperature=0.01, thinking=False)
            return resp if isinstance(resp, str) else (
                resp.get("content", "") if isinstance(resp, dict) else str(resp))
        except Exception as exc:  # noqa: BLE001
            logger.warning("AI 解读带诊断重试失败: %s", exc)
            return ""

    @staticmethod
    def _build_data_summary(dashboard_data: dict) -> str:
        """从 Dashboard 数据构建文本摘要。"""
        cards = dashboard_data.get("cards", [])
        parts = []
        for card in cards:
            title = card.get("title", "")
            data = card.get("data", {})
            rows = data.get("rows", []) if isinstance(data, dict) else []
            cols = data.get("columns", []) if isinstance(data, dict) else []
            parts.append(f"【{title}】{len(rows)}行数据，字段：{', '.join(cols[:5])}")
            if rows:
                sample = rows[:3]
                parts.append(f"  示例：{json.dumps(sample, ensure_ascii=False, default=str)[:200]}")
        return "\n".join(parts)[:2000]

    @staticmethod
    def _extract_metrics(dashboard_data: dict) -> list[dict]:
        """从 Dashboard 数据提取关键指标列表。"""
        metrics = []
        for card in dashboard_data.get("cards", []):
            data = card.get("data", {})
            if not isinstance(data, dict):
                continue
            cols = data.get("columns", [])
            rows = data.get("rows", [])
            if cols and rows:
                # 第一行各指标值
                for i, col in enumerate(cols[1:], 1):
                    val = rows[0][i] if i < len(rows[0]) else None
                    metrics.append({"name": col, "value": str(val), "card": card.get("title", "")})
        return metrics[:10]

    @staticmethod
    def _parse_result(text: str) -> InterpretationResult:
        """解析 LLM 输出为结构化结果（走统一 JSON 容错：围栏/尾逗号/单引号修复）。"""
        result = InterpretationResult()
        data = None
        try:
            parsed = extract_json(text or "")
            if isinstance(parsed, dict):
                data = parsed
        except Exception:  # noqa: BLE001
            data = None
        if isinstance(data, dict):
            result.summary = str(data.get("summary") or "")
            result.key_metrics = data.get("key_metrics") if isinstance(data.get("key_metrics"), list) else []
            result.trends = data.get("trends") if isinstance(data.get("trends"), list) else []
            result.comparisons = data.get("comparisons") if isinstance(data.get("comparisons"), list) else []
            result.anomalies = data.get("anomalies") if isinstance(data.get("anomalies"), list) else []
            result.suggestions = data.get("suggestions") if isinstance(data.get("suggestions"), list) else []
            # 至少有结论或任一结构化数组，才算解析成功
            if result.summary or any([result.key_metrics, result.trends,
                                      result.comparisons, result.anomalies, result.suggestions]):
                result.parse_ok = True
                return result
        # 结构化解析失败：全文截断兜底（降级档，parse_ok=False，上层会带诊断重试一次）
        result.summary = (text or "")[:500]
        result.parse_ok = False
        return result
