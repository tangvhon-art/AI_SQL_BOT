"""洞察分析生成器（公共层）。

六步向导：描述目的 → 选库 → AI生成草案 → 确认配置 → 预览报告 → 保存模板
本模块负责：根据目的+数据源生成分析项草案（InsightConfig），以及执行配置生成报告。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from ..models import Datasource, InsightTemplate, InsightReport, Report
from .ai_interpreter import AiInterpreter
from .chart_recommender import ChartRecommender
from .multi_query import ParallelExecutor
from .prompt_service import PromptService
from .llm_json import extract_json as _llm_extract_json

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
                 items: list[InsightItem] | None = None,
                 model_id: int | None = None):
        self.purpose = purpose
        self.datasource_id = datasource_id
        self.items = items or []
        self.model_id = model_id

    def to_dict(self) -> dict:
        return {"purpose": self.purpose, "datasource_id": self.datasource_id,
                "model_id": self.model_id,
                "items": [item.to_dict() for item in self.items]}

    @classmethod
    def from_dict(cls, d: dict) -> "InsightConfig":
        items = [InsightItem(**{k: v for k, v in item.items() if k in InsightItem.__init__.__code__.co_varnames})
                 for item in d.get("items", [])]
        return cls(purpose=d.get("purpose", ""), datasource_id=d.get("datasource_id", 0),
                   items=items, model_id=d.get("model_id"))


def _json_safe(obj: Any) -> Any:
    """递归将 date/datetime/Decimal 等不可 JSON 序列化的类型转为字符串。"""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


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

    def generate_draft(self, purpose: str, datasource_id: int, model_id: int | None = None) -> InsightConfig:
        """根据分析目的 + 数据源生成分析项草案。

        优先走多查询拆解（规则链 + LLM 兜底），将拆解出的子查询转为分析项；
        拆解失败时回退到直接 LLM 生成，再失败用默认模板。
        """
        ds = self.db.query(Datasource).get(datasource_id)
        if not ds:
            raise ValueError("数据源不存在")

        items: list[InsightItem] = []

        # 1. 多查询拆解（仅拆解，不做要素补齐/选表探测）
        try:
            from .multi_query import MultiQueryDecomposer
            engine = MultiQueryDecomposer(llm=self.llm)
            sub_questions = engine.decompose_questions(purpose)
            for i, q in enumerate(sub_questions):
                question = q.strip()
                if not question:
                    continue
                items.append(InsightItem(
                    id=f"item{i+1}",
                    title=question[:24],
                    question=question,
                    chart_type="bar",
                    enabled=True,
                ))
            logger.info("洞察草案 轻量拆解: %d 个子问题 → %d 个分析项", len(sub_questions), len(items))
        except Exception as exc:  # noqa: BLE001
            logger.warning("洞察草案 拆解失败，回退直接 LLM 生成: %s", exc)

        # 2. 拆解为空时回退直接 LLM 生成
        if not items:
            items = self._llm_generate_items(purpose, datasource_id)

        # 3. 仍为空时用默认模板
        if not items:
            items = self._default_items(purpose)

        return InsightConfig(purpose=purpose, datasource_id=datasource_id, items=items, model_id=model_id)

    def preview(self, config: InsightConfig, user_id: int = 0) -> list[dict]:
        """预览：对每个启用分析项走完整 AI 问数链路（要素提取 → 结构化 spec → NL2SQL → 查询）。"""
        from ..executor import run_query, SqlExecError
        from .nl2sql import generate_sql_stream
        from .query_spec import SubQuerySpec
        from .sub_spec import enrich_sub_specs

        enabled_items = [item for item in config.items if item.enabled]
        if not enabled_items:
            return []

        # 批量要素提取（与 AI 问数 Phase A 同一逻辑），为每个子问题构建结构化 spec
        sub_questions = [item.question for item in enabled_items]
        enriched: list[SubQuerySpec] = []
        try:
            enriched = enrich_sub_specs(
                question=config.purpose,
                sub_questions=sub_questions,
                parent_spec=None,
                llm=self.llm,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("洞察预览 要素提取失败，降级为纯问题生成: %s", exc)
            enriched = [SubQuerySpec(sub_id=f"q{i+1}", question=q) for i, q in enumerate(sub_questions)]

        results = []
        for idx, item in enumerate(enabled_items):
            card = {
                "id": item.id,
                "title": item.title,
                "question": item.question,
                "chart_type": item.chart_type,
                "sql": "",
                "columns": [],
                "rows": [],
                "status": "pending",
                "error": "",
            }
            try:
                sub = enriched[idx] if idx < len(enriched) else SubQuerySpec(
                    sub_id=item.id, question=item.question)
                # 用户在向导中选择的图表类型覆盖要素提取的 chart_hint
                if item.chart_type:
                    sub.chart_hint = item.chart_type
                # 构建 spec_context（与 AI 问数 N2 同一格式）
                try:
                    spec_context = {"spec": sub.to_query_spec().model_dump(mode="json"),
                                    "mapping": {}, "plan": {}}
                except Exception:  # noqa: BLE001
                    spec_context = {}
                table_hints = sub.table_hints or None
                question_text = item.question
                if sub.confirmed_tables:
                    question_text = f"{question_text}，已确认查询表：{'、'.join(sub.confirmed_tables)}"

                gen_result = None
                for event in generate_sql_stream(
                    config.datasource_id, self.workspace_id, question_text,
                    llm=self.llm,
                    table_hints=table_hints,
                    spec_context=spec_context,
                ):
                    if isinstance(event, dict) and event.get("type") == "result":
                        gen_result = event.get("result")
                        break
                if not gen_result:
                    card["status"] = "error"
                    card["error"] = "未能生成 SQL"
                    results.append(card)
                    continue
                sql = gen_result.get("sql", "")
                if not sql:
                    card["status"] = "error"
                    card["error"] = gen_result.get("explain") or "未能生成 SQL"
                    results.append(card)
                    continue
                card["sql"] = sql
                qr = run_query(config.datasource_id, sql, user_id)
                card["columns"] = qr.get("columns", [])
                card["rows"] = qr.get("rows", [])
                card["status"] = "success"
            except SqlExecError as exc:
                card["status"] = "error"
                card["error"] = str(exc)
            except Exception as exc:  # noqa: BLE001
                logger.warning("洞察预览 项=%s 执行失败: %s", item.id, exc)
                card["status"] = "error"
                card["error"] = str(exc)[:200]
            results.append(card)
        return results

    def execute(self, config: InsightConfig, template_id: int | None = None,
                prompt_template_id: int | None = None, created_by: int = 0) -> Report:
        """执行配置：实际执行各分析项 SQL → 汇总 Dashboard → AI解读 → 保存报告。"""
        enabled_items = [item for item in config.items if item.enabled]
        if not enabled_items:
            raise ValueError("没有启用的分析项")

        # 1. 实际执行各分析项（复用 preview 逻辑：NL2SQL 生成 + 执行查询）
        preview_cards = self.preview(config, user_id=created_by)
        dashboard_cards = []
        for pc in preview_cards:
            dashboard_cards.append({
                "sub_id": pc["id"],
                "title": pc["title"],
                "chart_type": pc["chart_type"],
                "data": {"columns": pc["columns"], "rows": pc["rows"]},
                "sql": pc["sql"],
                "status": pc["status"],
                "error": pc.get("error", ""),
            })

        layout = self.recommender.recommend_layout(
            len(dashboard_cards),
            [c.get("chart_type", "bar") for c in dashboard_cards],
        )
        dashboard_data = {"layout": layout, "cards": dashboard_cards,
                           "original_question": config.purpose}
        dashboard_data = _json_safe(dashboard_data)

        # 2. AI 解读
        interpretation = {}
        interpretation_text = ""
        if self.llm and self.llm.configured:
            prompt_svc = PromptService(self.db, workspace_id=self.workspace_id)
            interpreter = AiInterpreter(llm=self.llm, prompt_service=prompt_svc)
            result = interpreter.interpret(config.purpose, dashboard_data, prompt_template_id)
            interpretation = result.to_dict()
            interpretation_text = result.raw_text or result.summary

        # 3. AI 生成报告标题：洞察报告-{主题}相关报告-{YYYYMMDD}
        report_title = self._generate_report_title(config.purpose)

        # 4. 保存报告
        report = Report(
            workspace_id=self.workspace_id,
            title=report_title,
            original_question=config.purpose,
            multi_query_spec={},
            dashboard_data=dashboard_data,
            ai_interpretation=interpretation,
            interpretation_text=interpretation_text,
            created_by=created_by,
        )
        self.db.add(report)
        self.db.flush()

        # 5. 记录洞察报告
        insight_report = InsightReport(
            workspace_id=self.workspace_id,
            template_id=template_id,
            report_id=report.id,
            name=report.title,
            config_snapshot=_json_safe(config.to_dict()),
            status="success",
            created_by=created_by,
        )
        self.db.add(insight_report)
        self.db.commit()
        self.db.refresh(report)
        return report

    def _generate_report_title(self, purpose: str) -> str:
        """生成报告标题：洞察报告-{主题}相关报告-{YYYYMMDD}。优先 LLM 提取主题，失败时规则提取。"""
        from datetime import datetime
        import re
        date_str = datetime.now().strftime("%Y%m%d")

        # 规则提取候选主题：去掉查询动词/时间词/数量词，取第一个业务名词短语
        def _rule_topic(text: str) -> str:
            t = text
            for kw in ("查询", "统计", "分析", "展示", "列出", "获取", "计算", "求", "查看", "对比", "对比分析"):
                t = t.replace(kw, "")
            t = re.sub(r"近\s*\d+\s*[日天月年]", "", t)
            t = re.sub(r"(每[日天月年]|每日|每天|月度|每月|年度|今年|本月|本周|今日|昨天)", "", t)
            t = re.sub(r"(前\s*\d+|最多|最少|占比|排名|第\s*\d+|前三|前十)", "", t)
            t = re.sub(r"^各[部门个类种项]", "", t)  # 去掉"各部门"等限定
            t = re.sub(r"[，,。.；;、].*$", "", t)  # 取第一个分句
            t = re.sub(r"[的之与和或及\s]", "", t)
            return t[:8] if t else "综合分析"

        topic = _rule_topic(purpose)

        # LLM 增强：从返回中正则提取 2-8 个汉字的主题词
        _META_WORDS = {"核心业务主题词", "主题词", "业务主题", "分析目的", "核心主题",
                        "业务名词", "关键词", "核心词", "主题", "答案", "结果"}
        _GENERIC_WORDS = {"各部门", "员工", "用户", "数据", "业务", "流程", "记录",
                           "信息", "情况", "内容", "明细", "列表", "汇总", "统计"}
        if self.llm and self.llm.configured:
            try:
                resp = self.llm.chat([
                    {"role": "user", "content": f"从以下分析目的中提取一个2-8个汉字的核心业务主题词，只输出主题词：\n{purpose}"},
                ], temperature=0.1, max_tokens=100)
                matches = re.findall(r"[\u4e00-\u9fa5]{2,8}", resp or "")
                candidates = [m for m in matches
                              if m not in _META_WORDS
                              and m not in _GENERIC_WORDS
                              and len(m) >= 3
                              and not any(v in m for v in (
                                  "查询", "统计", "分析", "展示", "获取", "计算", "思考", "过程",
                                  "输出", "只输", "请输", "提取", "核心业", "业务主", "主题词"))]
                if candidates:
                    # 取最长的候选（更具体），与规则结果比较取更长的
                    llm_topic = max(candidates, key=len)
                    if len(llm_topic) >= len(topic):
                        topic = llm_topic
            except Exception:  # noqa: BLE001
                pass

        return f"洞察报告-{topic}相关报告-{date_str}"

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
        valid_charts = {"bar", "line", "pie", "radar", "combo",
                        "stack_bar", "group_bar", "rank", "kpi", "table"}

        def _build_prompt(diag: str = "") -> str:
            head = (
                f"你是一位数据分析师。请根据以下分析目的，设计 3-6 个分析项。\n"
                f"分析目的：{purpose}\n"
                f"数据源ID：{datasource_id}\n\n"
                "【输出协议】只输出一个 JSON 对象，键名固定为 items；禁止 Markdown 围栏与解释文字。\n"
                "items 为数组，每个元素键名固定为 id/title/question/chart_type：\n"
                "- id：item1、item2…；title：简洁中文标题；question：可独立执行的自然语言查询问题，必填非空；\n"
                "- chart_type 只能取 bar/line/pie/radar/combo/stack_bar/group_bar/rank/kpi/table 之一；\n"
                "- 至少返回 3 个分析项，覆盖总体/趋势/对比或排行等不同角度；\n"
                '格式：{"items": [{"id":"item1","title":"","question":"","chart_type":"bar"}]}\n'
            )
            if diag:
                head += f"\n【输出修正】{diag}，请重新输出完整合法 JSON。\n"
            return head

        def _parse_items(text: str) -> list[InsightItem]:
            data = _llm_extract_json(text)  # 统一容错（围栏/尾逗号/单引号修复）
            if not isinstance(data, dict):
                return []
            items_data = data.get("items", [])
            if not isinstance(items_data, list):
                return []
            items = []
            for i, item in enumerate(items_data[:8]):
                if not isinstance(item, dict):
                    continue
                question = str(item.get("question") or "").strip()
                title = str(item.get("title") or "").strip()
                if not question:  # question 是后续执行的必填项，缺失元素丢弃但保留其余（部分可用）
                    continue
                ct = str(item.get("chart_type") or "bar").strip()
                if ct not in valid_charts:
                    ct = "bar"
                items.append(InsightItem(
                    id=str(item.get("id") or f"item{i+1}"),
                    title=title or f"分析项{i+1}",
                    question=question,
                    chart_type=ct,
                ))
            return items

        text = ""
        for attempt in range(2):  # 首轮 + 带诊断重试 1 次
            try:
                diag = ""
                if attempt == 1:
                    if not text:
                        diag = "上一次返回为空"
                    else:
                        diag = "上一次输出不是约定的 items JSON（可能含围栏/尾逗号/缺字段）"
                resp = self.llm.chat(_build_prompt(diag), temperature=0.3)
                text = resp if isinstance(resp, str) else getattr(resp, "content", str(resp))
                items = _parse_items(text)
                if items:
                    return items
            except Exception as exc:  # noqa: BLE001
                logger.warning("洞察草案 LLM 第 %d 次生成失败: %s", attempt + 1, exc)
        logger.warning("洞察草案 LLM 两次均失败/无有效分析项，回退默认模板")
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
