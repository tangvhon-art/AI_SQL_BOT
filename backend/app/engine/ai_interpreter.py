"""AI 解读引擎（公共层）。

基于 Dashboard 数据 + 公共 Prompt 模板，生成结构化解读结果。
支持流式输出（SSE 推送），结果可保存为报告。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Generator

from ..models import Prompt
from .prompt_service import PromptService

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

        # 4. 调用 LLM（流式）
        full_text = ""
        try:
            for delta in self._llm_stream(prompt):
                full_text += delta
                yield {"type": "ai_interpretation", "delta": delta, "raw_text": full_text}
        except Exception as exc:  # noqa: BLE001
            logger.warning("AI 解读 LLM 调用失败: %s", exc)
            yield {"type": "ai_interpretation_error", "error": str(exc)}
            return

        # 5. 解析结构化结果
        result = self._parse_result(full_text)
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

    def _llm_stream(self, prompt: str) -> Generator[str, None, None]:
        """调用 LLM 流式接口，yield 增量文本。"""
        messages = [{"role": "user", "content": prompt}]
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
            "你是一位资深数据分析师。请基于以下数据进行分析：\n"
            "原始问题：{{question}}\n"
            "数据摘要：{{data_summary}}\n"
            "关键指标：{{metrics}}\n\n"
            "请按以下结构输出分析结果（JSON）：\n"
            '{"summary": "总体结论", "key_metrics": [{"name":"","value":"","change":""}], '
            '"trends": [], "comparisons": [], "anomalies": [{"desc":"","severity":"high/medium/low"}], '
            '"suggestions": []}'
        )

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
        """解析 LLM 输出为结构化结果。"""
        result = InterpretationResult()
        # 尝试提取 JSON
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                data = json.loads(m.group(0))
                result.summary = data.get("summary", "")
                result.key_metrics = data.get("key_metrics", [])
                result.trends = data.get("trends", [])
                result.comparisons = data.get("comparisons", [])
                result.anomalies = data.get("anomalies", [])
                result.suggestions = data.get("suggestions", [])
                return result
            except json.JSONDecodeError:
                pass
        # JSON 解析失败，全文作为 summary
        result.summary = text[:500]
        return result
