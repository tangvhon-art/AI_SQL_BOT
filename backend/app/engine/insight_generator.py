"""洞察分析生成器（公共层）。

六步向导：描述目的 → 选库 → AI生成草案 → 确认配置 → 预览报告 → 保存模板
本模块负责：根据目的+数据源生成分析项草案（InsightConfig），以及执行配置生成报告。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy.orm import Session

from ..models import Datasource, InsightTemplate, InsightReport, Report
from .ai_interpreter import AiInterpreter
from .chart_recommender import ChartRecommender
from .multi_query import ParallelExecutor
from .prompt_service import PromptService

logger = logging.getLogger(__name__)


class InsightItem:
    """单个分析项配置。"""

    def __init__(self, id: str = "", title: str = "", question: str = "",
                 chart_type: str = "bar", enabled: bool = True):
        self.id = id
        self.title = title
        self.question = question
        self.chart_type = chart_type
        self.enabled = enabled

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "question": self.question,
                "chart_type": self.chart_type, "enabled": self.enabled}


class InsightConfig:
    """洞察分析完整配置。"""

    def __init__(self, purpose: str = "", datasource_id: int = 0,
                 items: list[InsightItem] | None = None):
        self.purpose = purpose
        self.datasource_id = datasource_id
        self.items = items or []

    def to_dict(self) -> dict:
        return {"purpose": self.purpose, "datasource_id": self.datasource_id,
                "items": [item.to_dict() for item in self.items]}

    @classmethod
    def from_dict(cls, d: dict) -> "InsightConfig":
        items = [InsightItem(**{k: v for k, v in item.items() if k in InsightItem.__init__.__code__.co_varnames})
                 for item in d.get("items", [])]
        return cls(purpose=d.get("purpose", ""), datasource_id=d.get("datasource_id", 0), items=items)


class InsightGenerator:
    """洞察分析生成器。

    用法：
        gen = InsightGenerator(db, llm=llm, workspace_id=1)
        draft = gen.generate_draft(purpose="分析门店经营状况", datasource_id=1)
        # 用户确认配置后：
        report = gen.execute(config, template_id=123)
    """

    def __init__(self, db: Session, llm: Any | None = None, workspace_id: int = 0):
        self.db = db
        self.llm = llm
        self.workspace_id = workspace_id
        self.recommender = ChartRecommender()

    def generate_draft(self, purpose: str, datasource_id: int) -> InsightConfig:
        """根据分析目的 + 数据源生成分析项草案。"""
        ds = self.db.query(Datasource).get(datasource_id)
        if not ds:
            raise ValueError("数据源不存在")

        # 尝试 LLM 生成
        items = self._llm_generate_items(purpose, datasource_id)

        # LLM 失败或返回空时，使用默认模板
        if not items:
            items = self._default_items(purpose)

        return InsightConfig(purpose=purpose, datasource_id=datasource_id, items=items)

    def execute(self, config: InsightConfig, template_id: int | None = None,
                prompt_template_id: int | None = None, created_by: int = 0) -> Report:
        """执行配置：并行执行各分析项 → 汇总 Dashboard → AI解读 → 保存报告。"""
        enabled_items = [item for item in config.items if item.enabled]
        if not enabled_items:
            raise ValueError("没有启用的分析项")

        # 1. 并行执行各分析项（此处简化为生成占位结果，实际应调用 nl2sql + 执行）
        dashboard_cards = []
        for item in enabled_items:
            dashboard_cards.append({
                "sub_id": item.id,
                "title": item.title,
                "chart_type": item.chart_type,
                "data": {"columns": ["维度", "数值"], "rows": [["示例", 0]]},
                "sql": "",
                "status": "success",
            })

        layout = self.recommender.recommend_layout(
            len(dashboard_cards),
            [c.get("chart_type", "bar") for c in dashboard_cards],
        )
        dashboard_data = {"layout": layout, "cards": dashboard_cards,
                           "original_question": config.purpose}

        # 2. AI 解读
        interpretation = {}
        interpretation_text = ""
        if self.llm and self.llm.configured:
            prompt_svc = PromptService(self.db, workspace_id=self.workspace_id)
            interpreter = AiInterpreter(llm=self.llm, prompt_service=prompt_svc)
            result = interpreter.interpret(config.purpose, dashboard_data, prompt_template_id)
            interpretation = result.to_dict()
            interpretation_text = result.raw_text or result.summary

        # 3. 保存报告
        report = Report(
            workspace_id=self.workspace_id,
            title=f"洞察报告：{config.purpose[:30]}",
            original_question=config.purpose,
            multi_query_spec={},
            dashboard_data=dashboard_data,
            ai_interpretation=interpretation,
            interpretation_text=interpretation_text,
            created_by=created_by,
        )
        self.db.add(report)
        self.db.flush()

        # 4. 记录洞察报告
        insight_report = InsightReport(
            workspace_id=self.workspace_id,
            template_id=template_id,
            report_id=report.id,
            name=report.title,
            config_snapshot=config.to_dict(),
            status="success",
            created_by=created_by,
        )
        self.db.add(insight_report)
        self.db.commit()
        self.db.refresh(report)
        return report

    def save_template(self, config: InsightConfig, name: str,
                      description: str = "", prompt_template_id: int | None = None,
                      created_by: int = 0) -> InsightTemplate:
        """保存配置为模板，可重复生成报告。"""
        tpl = InsightTemplate(
            workspace_id=self.workspace_id,
            name=name,
            description=description,
            purpose=config.purpose,
            datasource_id=config.datasource_id,
            config=config.to_dict(),
            prompt_template_id=prompt_template_id,
            created_by=created_by,
        )
        self.db.add(tpl)
        self.db.commit()
        self.db.refresh(tpl)
        return tpl

    # ---------- 内部方法 ----------

    def _llm_generate_items(self, purpose: str, datasource_id: int) -> list[InsightItem]:
        """LLM 生成分析项草案。"""
        if not self.llm or not self.llm.configured:
            return []
        try:
            prompt = (
                f"你是一位数据分析师。请根据以下分析目的，设计 3-6 个分析项。\n"
                f"分析目的：{purpose}\n"
                f"数据源ID：{datasource_id}\n\n"
                f"每个分析项包含：id(如item1)、title(分析项标题)、question(自然语言查询问题)、"
                f"chart_type(推荐图表类型：bar/line/pie/radar/combo/stack_bar/rank/kpi/table)。\n\n"
                f"请严格按 JSON 格式返回：{{\"items\": [{{\"id\":\"\",\"title\":\"\",\"question\":\"\",\"chart_type\":\"\"}}]}}\n"
                f"只返回 JSON，不要其他文字。"
            )
            resp = self.llm.chat(prompt, temperature=0.3)
            text = resp if isinstance(resp, str) else getattr(resp, "content", str(resp))
            m = re.search(r"\{[\s\S]*\}", text)
            if not m:
                return []
            data = json.loads(m.group(0))
            items_data = data.get("items", [])
            if not isinstance(items_data, list):
                return []
            items = []
            for i, item in enumerate(items_data[:8]):
                items.append(InsightItem(
                    id=item.get("id", f"item{i+1}"),
                    title=item.get("title", f"分析项{i+1}"),
                    question=item.get("question", ""),
                    chart_type=item.get("chart_type", "bar"),
                ))
            return items
        except Exception as exc:  # noqa: BLE001
            logger.warning("洞察草案 LLM 生成失败: %s", exc)
            return []

    @staticmethod
    def _default_items(purpose: str) -> list[InsightItem]:
        """默认分析项模板（LLM 不可用时的兜底）。"""
        return [
            InsightItem(id="item1", title="总体概览", question=f"{purpose}的总体情况", chart_type="kpi"),
            InsightItem(id="item2", title="趋势分析", question=f"{purpose}的变化趋势", chart_type="line"),
            InsightItem(id="item3", title="对比分析", question=f"{purpose}的分类对比", chart_type="bar"),
            InsightItem(id="item4", title="排行分析", question=f"{purpose}的排行榜", chart_type="rank"),
        ]
