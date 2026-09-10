"""多查询拆解与并行执行（C11 公共层）。

MultiQueryDecomposer：策略模式 + 责任链，规则拆解优先，LLM 兜底。
ParallelExecutor：泛型并行执行模板（V1.0 遗留，多查询 V2.0 改用 MultiQueryOrchestrator）。
MultiQueryOrchestrator：多查询执行编排器（asyncio + 信号量），并行调度 N 个子查询，
统一并发上限 / 超时 / 容错 / 取消 / 进度回调。
"""
from __future__ import annotations

import asyncio
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import logging

from .query_spec import MultiQuerySpec, SubQuerySpec

logger = logging.getLogger(__name__)


def _new_task_id() -> str:
    return uuid.uuid4().hex[:12]


# 子问题前导连接词（逗号/标点拆解后残留，需清洗：如「以及流程发起占比…」「并分析…」）
_SUB_LEAD_CONN = ("以及", "并且", "还有", "同时", "另外", "再然后", "然后", "并", "且", "再", "也")


def _clean_sub_question(p: str) -> str:
    """清洗拆解后的子问题：去前导连接词、重复查询动词、多余标点。"""
    p = p.strip("，,。.；; \t")
    for conn in _SUB_LEAD_CONN:
        if p.startswith(conn):
            p = p[len(conn):].lstrip("，,。.；; \t")
            break
    # 去重复查询动词（「查询近7日查询…」→「查询近7日…」）
    for verb in ("查询", "统计", "分析", "查看"):
        m = re.match(rf"^({verb}[^，,。]{0,10}?)({verb})", p)
        if m:
            p = m.group(1) + p[len(m.group(1)) + len(m.group(2)):]
            break
    return p.strip("，,。.；; \t")


# ---------- 拆解规则接口 ----------
class DecomposeRule:
    """拆解规则接口：match 判断是否命中，decompose 返回子问题列表。"""

    def match(self, question: str) -> bool:
        raise NotImplementedError

    def decompose(self, question: str) -> list[str]:
        raise NotImplementedError


class SemicolonRule(DecomposeRule):
    """分号/句号分隔：问题中含 ; ； 。 且每段都是独立查询。
    末尾的「两个问题」「三个问题」等显式标记不作为独立查询。"""

    _SEP = re.compile(r"[;；。]")
    # 末尾标记（不计为独立查询）
    _TAIL_MARK = re.compile(r"^(这?是?)?[两二三四五六七八九十\d]+个?问题$")

    def match(self, question: str) -> bool:
        parts = [p.strip() for p in self._SEP.split(question) if p.strip()]
        # 过滤末尾标记
        parts = [p for p in parts if not self._TAIL_MARK.match(p)]
        return len(parts) >= 2 and all(len(p) > 4 for p in parts)

    def decompose(self, question: str) -> list[str]:
        parts = [p.strip() for p in self._SEP.split(question) if p.strip()]
        return [p for p in parts if not self._TAIL_MARK.match(p)]


class ExplicitMultiQueryRule(DecomposeRule):
    """显式多查询标记：「两个问题」「三个问题」「N个问题」「分别查询」「分别统计」。
    优先级最高，命中后按并列连词/逗号拆分。"""

    _EXPLICIT = re.compile(r"[两二三四五六七八九十\d]+个?问题|分别(?:查询|统计|看|分析)|分[两二]步|两个维度")
    _SPLIT = re.compile(r"(?:以及|还有|并且|，|,|；|;)")

    def match(self, question: str) -> bool:
        if not self._EXPLICIT.search(question):
            return False
        parts = self._split(question)
        return len(parts) >= 2

    def decompose(self, question: str) -> list[str]:
        return self._split(question)

    def _split(self, question: str) -> list[str]:
        # 去掉末尾的显式标记
        q = self._EXPLICIT.sub("", question).strip("，,。.；; ")
        parts = [p.strip("，,。.；; \t") for p in self._SPLIT.split(q) if p.strip("，,。.；; \t") and len(p.strip("，,。.；; \t")) > 3]
        # 补全公共前缀（时间范围）
        if len(parts) >= 2:
            prefix = ""
            m = re.search(r"^(.*?(?:今天|本月|今年|近\d+[天日]|本季度|上周|昨天|当日|当天))", parts[0])
            if m:
                prefix = m.group(1)
            for i in range(1, len(parts)):
                if prefix and not any(t in parts[i] for t in ("今天", "本月", "今年", "近", "本季度", "上周", "昨天", "当日", "当天")):
                    parts[i] = f"{prefix}{parts[i]}"
        return parts


class CommaSplitRule(DecomposeRule):
    """逗号分隔 + 双指标词：如「查询近7日每天发起量,流程发起占比最多的流程有多少个」。
    仅当逗号前后两段都包含指标词时才触发，避免误拆句子内部停顿。"""

    _SEP = re.compile(r"[，,]")
    # 复用指标触发词
    _METRIC_WORDS = ("数量", "金额", "销售额", "订单量", "客单价", "占比", "增长率",
                     "趋势", "排行", "排名", "分布", "统计", "对比", "平均", "总量",
                     "发起量", "流程数", "审批量", "处理量", "通过量", "驳回量",
                     "完成量", "新增量", "增长量", "下降量", "最大值", "最小值",
                     "个数", "次数", "人数", "笔数", "件数", "总数", "多少", "几个")

    def match(self, question: str) -> bool:
        parts = [p.strip("，,。.；; \t") for p in self._SEP.split(question) if p.strip("，,。.；; \t")]
        if len(parts) < 2:
            return False
        # 每段长度 > 6 且至少含 1 个指标词
        for i, p in enumerate(parts):
            if len(p) <= 6:
                return False
            if not any(w in p for w in self._METRIC_WORDS):
                return False
            # 防误判：非首段以「按」开头（如"按天统计""按部门分组"）通常是补充说明，不是独立查询
            if i > 0 and p.startswith("按"):
                return False
        return True

    def decompose(self, question: str) -> list[str]:
        parts = [p.strip("，,。.；; \t") for p in self._SEP.split(question) if p.strip("，,。.；; \t")]
        # 补全公共前缀（时间范围）
        prefix = ""
        m = re.search(r"^(.*?(?:今天|当日|当天|本月|今年|近\d+[天日]|本季度|上周|昨天))", parts[0])
        if m:
            prefix = m.group(1)
        result = []
        for i, p in enumerate(parts):
            if i == 0:
                result.append(p)
            else:
                # 先清洗（去前导连接词等），再补公共时间前缀，避免连接词被挤到前缀之后残留
                cleaned = _clean_sub_question(p)
                sub = f"{prefix}{cleaned}" if prefix and not any(
                    t in cleaned for t in ("今天", "本月", "今年", "近", "本季度", "上周", "昨天", "当日", "当天")) else cleaned
                result.append(sub)
        return result


class ParallelConjunctionRule(DecomposeRule):
    """并列连词 + 指标词：如「查询销售额和订单量以及客单价」「发起量以及占比」。"""

    _CONJ = re.compile(r"(?:和|以及|还有|同时|并且)")
    # 指标触发词（覆盖业务常见表达）
    _METRIC_WORDS = ("数量", "金额", "销售额", "订单量", "客单价", "占比", "增长率",
                     "趋势", "排行", "排名", "分布", "统计", "对比", "平均", "总量",
                     "发起量", "流程数", "审批量", "处理量", "通过量", "驳回量",
                     "完成量", "新增量", "增长量", "下降量", "最大值", "最小值",
                     "个数", "次数", "人数", "笔数", "件数", "总数")

    def match(self, question: str) -> bool:
        if not self._CONJ.search(question):
            return False
        # 至少出现 1 个指标触发词（降低阈值，业务表达多样）
        hits = sum(1 for w in self._METRIC_WORDS if w in question)
        return hits >= 1

    def decompose(self, question: str) -> list[str]:
        """按并列连词拆分，每段保留公共前缀（如时间/范围）。"""
        parts = self._CONJ.split(question)
        parts = [p.strip("，,。.；; \t") for p in parts if p.strip("，,。.；; \t")]
        if len(parts) < 2:
            return [question]
        # 提取公共前缀（第一段中时间/范围表达）
        prefix = ""
        m = re.search(r"^(.*?(?:今天|本月|今年|近\d+[天日]|本季度|上周|昨天|当日|当天))", parts[0])
        if m:
            prefix = m.group(1)
        result = []
        for i, p in enumerate(parts):
            if i == 0:
                result.append(p)
            else:
                sub = f"{prefix}{p}" if prefix and not any(
                    t in p for t in ("今天", "本月", "今年", "近", "本季度", "上周", "昨天", "当日", "当天")) else p
                result.append(sub)
        return result


class MultiDimensionRule(DecomposeRule):
    """「同时」+ 多维度：如「同时看各区域和各门店的表现」。"""

    def match(self, question: str) -> bool:
        return ("同时" in question or "分别" in question) and (
            "各" in question or "每个" in question or "按" in question)

    def decompose(self, question: str) -> list[str]:
        # 提取维度列表
        dims = re.findall(r"各(\w+?)(?:的|和|以及|$)", question)
        if len(dims) < 2:
            return [question]
        base = re.sub(r"(?:同时|分别).*$", "", question).strip()
        return [f"{base}，按{d}维度" for d in dims]


# ---------- 公共拆解器 ----------
class MultiQueryDecomposer:
    """多查询拆解器：规则链优先，LLM 兜底。

    用法：
        decomposer = MultiQueryDecomposer(llm=llm_client)
        spec = decomposer.decompose(question, workspace_id, datasource_id)
    """

    _rules: list[DecomposeRule] = [
        ExplicitMultiQueryRule(),   # 优先级最高：显式多查询标记
        SemicolonRule(),            # 分号/句号分隔
        CommaSplitRule(),           # 逗号分隔+双指标词
        ParallelConjunctionRule(),  # 并列连词+指标词
        MultiDimensionRule(),       # 同时/分别+多维度
    ]

    def __init__(self, llm: Any | None = None):
        self.llm = llm

    @classmethod
    def register_rule(cls, rule: DecomposeRule, priority: int = 0) -> None:
        """注册新拆解规则（priority 越大越靠前）。"""
        cls._rules.append(rule)
        cls._rules.sort(key=lambda r: getattr(r, "_priority", 0), reverse=True)

    def decompose(self, question: str, workspace_id: int = 0,
                  datasource_id: int = 0, parent_spec: Any | None = None,
                  task_id: str = "") -> MultiQuerySpec:
        """拆解入口：规则链 → LLM 兜底 → 要素补齐 → 组装 MultiQuerySpec。

        parent_spec：父级 QuerySpec（时间/表提示等上下文继承给子查询）；
        task_id：两阶段编排任务标识（multi_spec 事件下发，confirm 幂等用）。
        """
        logger.info("[多查询拆解] 原始问题: %s", question[:120])

        # 1. 规则链
        sub_questions: list[str] | None = None
        source = "rule"
        hit_rule = None
        for rule in self._rules:
            rule_name = type(rule).__name__
            matched = rule.match(question)
            logger.info("[多查询拆解] 规则 %s: match=%s", rule_name, matched)
            if matched:
                sub_questions = rule.decompose(question)
                logger.info("[多查询拆解] 规则 %s 拆解结果: %s", rule_name, sub_questions)
                if sub_questions and len(sub_questions) >= 2:
                    hit_rule = rule_name
                    break
                sub_questions = None

        # 2. LLM 兜底
        if not sub_questions or len(sub_questions) < 2:
            logger.info("[多查询拆解] 规则链未命中或不足2个，走 LLM 兜底")
            sub_questions = self._llm_decompose(question)
            if sub_questions and len(sub_questions) >= 2:
                source = "llm"
                hit_rule = "llm_fallback"
                logger.info("[多查询拆解] LLM 兜底拆解结果: %s", sub_questions)
            else:
                source = "single"
                logger.info("[多查询拆解] LLM 兜底也未识别出多查询，按单查询处理")

        # 2.5 智能降级：规则+LLM 都失败，但问题含 ≥2 个指标词时，按逗号/句号最后一次简单拆分
        if (not sub_questions or len(sub_questions) < 2) and source == "single":
            _metric_words = ("数量", "金额", "销售额", "订单量", "客单价", "占比", "增长率",
                             "趋势", "排行", "排名", "分布", "统计", "对比", "平均", "总量",
                             "发起量", "流程数", "审批量", "处理量", "通过量", "驳回量",
                             "完成量", "新增量", "增长量", "下降量", "最大值", "最小值",
                             "个数", "次数", "人数", "笔数", "件数", "总数", "多少", "几个")
            _hits = sum(1 for w in _metric_words if w in question)
            if _hits >= 2:
                _parts = [p.strip("，,。.；; \t") for p in re.split(r"[，,。.；;]", question)
                          if p.strip("，,。.；; \t") and len(p.strip("，,。.；; \t")) > 6]
                # 清洗前导连接词（如「并分析…」「以及…」），避免残留在子问题中
                _parts = [_clean_sub_question(p) for p in _parts]
                # 每段都必须含指标词，且非首段不以「按」开头（防误拆补充说明）
                if len(_parts) >= 2 and all(
                    any(w in p for w in _metric_words) for p in _parts
                ) and not any(
                    i > 0 and p.startswith("按") for i, p in enumerate(_parts)
                ):
                    sub_questions = _parts
                    source = "fallback_split"
                    hit_rule = "metric_fallback"
                    logger.info("[多查询拆解] 智能降级（%d 个指标词）按标点拆分: %s", _hits, _parts)

        # 3. 单查询兼容：只有一个子问题时返回长度=1
        if not sub_questions:
            sub_questions = [question]

        # 4. 组装 SubQuerySpec（要素补齐：LLM 批量提取 + 规则校验 + 父级上下文继承）
        from .sub_spec import enrich_sub_specs
        sub_questions = [q.strip() for q in sub_questions if q.strip()]
        sub_specs = enrich_sub_specs(
            question=question,
            sub_questions=sub_questions,
            parent_spec=parent_spec,
            llm=self.llm,
            parent_hints=getattr(parent_spec, "table_hints", None) if parent_spec else None,
            schema_name=(getattr(parent_spec, "schema_name", "") or "") if parent_spec else "",
        )

        logger.info("[多查询拆解] 最终: source=%s hit_rule=%s sub_count=%d subs=%s",
                    source, hit_rule, len(sub_specs),
                    [s.question[:40] for s in sub_specs])

        # 选表澄清探测：预判子查询执行时是否会触发选表歧义（用户确认前先澄清）
        if sub_specs:
            try:
                from .sub_spec import probe_table_clarify
                for s in sub_specs:
                    probe = probe_table_clarify(
                        s, datasource_id, llm=self.llm)
                    if probe.get("needs"):
                        s.needs_tables = True
                        s.candidate_tables = probe.get("candidates") or []
                        logger.info("[多查询拆解] 子查询 %s 需澄清选表（%s），候选 %d 张: %s",
                                    s.sub_id, probe.get("reason"), len(s.candidate_tables),
                                    [c.get("table") for c in s.candidate_tables][:8])
            except Exception as exc:  # noqa: BLE001
                logger.warning("[多查询拆解] 选表探测失败（跳过澄清）: %s", exc)

        return MultiQuerySpec(
            original_question=question,
            sub_queries=sub_specs,
            layout_hint="auto",
            source=source,
            task_id=task_id or _new_task_id(),
        )

    def _llm_decompose(self, question: str) -> list[str] | None:
        """LLM 兜底拆解：返回子问题列表，失败返回 None。"""
        if not self.llm:
            return None
        try:
            prompt = (
                "你是一个查询拆解助手。请判断以下自然语言问题是否包含多个独立的数据查询意图。\n"
                "如果包含多个意图，请拆解为多个独立的子问题，每个子问题可以独立执行 SQL 查询。\n"
                "如果只有一个意图，返回原问题。\n\n"
                "【重要判断规则】\n"
                "1. 问题中出现「两个问题」「三个问题」「N个问题」「分别查询」「分别统计」等表述时，必须拆解为多个子问题。\n"
                "2. 问题中出现「以及」「还有」「并且」等并列连词，且前后是不同的数据指标（如「发起量」和「占比」）时，必须拆解。\n"
                "3. 每个子问题必须是完整的、可独立执行的查询，保留公共的时间范围/条件。\n"
                "4. 拆解时去掉「两个问题」「三个问题」等标记性表述。\n\n"
                f"问题：{question}\n\n"
                "请严格按 JSON 格式返回：{\"sub_questions\": [\"子问题1\", \"子问题2\", ...]}\n"
                "只返回 JSON，不要其他文字。"
            )
            resp = self.llm.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.1, json_mode=True, thinking=False,
            )
            text = resp if isinstance(resp, str) else getattr(resp, "content", str(resp))
            # 提取 JSON
            m = re.search(r"\{[\s\S]*\}", text)
            if not m:
                return None
            import json
            data = json.loads(m.group(0))
            subs = data.get("sub_questions", [])
            return [s.strip() for s in subs if s.strip()] if isinstance(subs, list) else None
        except Exception:
            return None


# ---------- 公共并行执行器 ----------
class ParallelExecutor:
    """泛型并行执行模板：统一管理并发度、超时、容错、进度回调。

    用法：
        executor = ParallelExecutor(max_workers=5, timeout_s=30.0)
        results = executor.run(
            tasks=[("q1", lambda: do_work(1)), ("q2", lambda: do_work(2))],
            on_result=lambda sub_id, result: print(f"{sub_id} done"),
            on_error=lambda sub_id, exc: print(f"{sub_id} failed: {exc}"),
        )
        # results = {"q1": result1, "q2": exception2}
    """

    def __init__(self, max_workers: int = 5, timeout_s: float = 30.0):
        self.max_workers = max_workers
        self.timeout_s = timeout_s

    def run(
        self,
        tasks: list[tuple[str, Callable]],
        on_result: Callable[[str, Any], None] | None = None,
        on_error: Callable[[str, Exception], None] | None = None,
    ) -> dict[str, Any]:
        """并行执行任务列表，返回 {sub_id: result | exception}。

        单任务超时不阻塞其他，失败结果封装为异常对象返回。
        on_result/on_error 回调用于实时推送（如 SSE 事件）。
        """
        results: dict[str, Any] = {}
        if not tasks:
            return results

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            results = loop.run_until_complete(self._run_async(tasks, on_result, on_error))
        finally:
            loop.close()
        return results

    async def _run_async(
        self,
        tasks: list[tuple[str, Callable]],
        on_result: Callable[[str, Any], None] | None,
        on_error: Callable[[str, Exception], None] | None,
    ) -> dict[str, Any]:
        results: dict[str, Any] = {}
        loop = asyncio.get_running_loop()
        thread_pool = ThreadPoolExecutor(max_workers=self.max_workers)

        async def run_one(sub_id: str, fn: Callable) -> None:
            try:
                result = await asyncio.wait_for(
                    loop.run_in_executor(thread_pool, fn),
                    timeout=self.timeout_s,
                )
                results[sub_id] = result
                if on_result:
                    on_result(sub_id, result)
            except asyncio.TimeoutError:
                exc = TimeoutError(f"子查询 {sub_id} 执行超时（{self.timeout_s}s）")
                results[sub_id] = exc
                if on_error:
                    on_error(sub_id, exc)
            except Exception as exc:  # noqa: BLE001
                results[sub_id] = exc
                if on_error:
                    on_error(sub_id, exc)

        await asyncio.gather(*[run_one(sid, fn) for sid, fn in tasks])
        thread_pool.shutdown(wait=False)
        return results


# ---------- 多查询 V2.0 执行编排器 ----------
class MultiQueryOrchestrator:
    """多查询执行编排器：并行调度 N 个子查询流水线。

    对比 V1.0 ParallelExecutor 的差异：
    - 真并行：asyncio.gather + Semaphore 并发调度（子任务在独立事件循环线程内执行）
    - 统一超时：单子查询超时 multi_sub_timeout_s，超时置 error 不阻塞其余
    - 整体取消：cancel() 置标志，未完成任务置 cancelled，已完成卡片保留
    - 进度回调：on_event(event, data) 供 SSE 实时推送

    用法：
        orch = MultiQueryOrchestrator(max_concurrent=4, sub_timeout_s=60.0)
        results = orch.run(sub_queries, ctx)   # ctx: 见 sub_task.SubTaskContext
        orch.cancel()                          # 任意时刻调用
    """

    def __init__(self, max_concurrent: int = 4, sub_timeout_s: float = 60.0):
        self.max_concurrent = max_concurrent
        self.sub_timeout_s = sub_timeout_s
        self._cancelled = False
        self._lock = threading.Lock()

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        """请求整体取消：未完成任务置 cancelled，已完成卡片保留。"""
        with self._lock:
            self._cancelled = True

    def run(self, sub_queries: list[SubQuerySpec], ctx: Any) -> dict[str, Any]:
        """并行执行子查询列表。返回 {sub_id: SubTaskResult}。

        ctx 需提供：emit(event, data)、run_subtask_pipeline 所需全部字段。
        同步阻塞直到全部子查询结束（含取消）。
        """
        from .sub_task import run_subtask_pipeline, SubTaskResult

        results: dict[str, Any] = {}
        if not sub_queries:
            return results

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            results = loop.run_until_complete(
                self._run_async(sub_queries, ctx, run_subtask_pipeline, SubTaskResult))
        finally:
            loop.close()
        return results

    async def _run_async(self, sub_queries, ctx, runner, result_cls) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        sem = asyncio.Semaphore(self.max_concurrent)
        results: dict[str, Any] = {}

        async def run_one(sub: SubQuerySpec) -> None:
            async with sem:
                if self.cancelled:
                    ctx.emit("sub_error", {"sub_id": sub.sub_id,
                                           "error": "已取消", "retryable": False,
                                           "stage": "cancelled"})
                    return
                try:
                    result = await asyncio.wait_for(
                        loop.run_in_executor(None, runner, sub, ctx),
                        timeout=self.sub_timeout_s)
                    results[sub.sub_id] = result
                except asyncio.TimeoutError:
                    exc = TimeoutError(f"子查询 {sub.sub_id} 执行超时（{self.sub_timeout_s}s）")
                    results[sub.sub_id] = exc
                    ctx.emit("sub_error", {"sub_id": sub.sub_id, "error": str(exc),
                                           "retryable": True, "stage": "timeout"})
                except Exception as exc:  # noqa: BLE001
                    results[sub.sub_id] = exc
                    ctx.emit("sub_error", {"sub_id": sub.sub_id, "error": str(exc),
                                           "retryable": True, "stage": "pipeline"})

        await asyncio.gather(*[run_one(s) for s in sub_queries])

        # 取消兜底：未完成（超时中断/排队中被取消）的子查询补齐 cancelled 结果
        for s in sub_queries:
            if s.sub_id not in results:
                results[s.sub_id] = result_cls(s.sub_id, status="cancelled")
        return results
