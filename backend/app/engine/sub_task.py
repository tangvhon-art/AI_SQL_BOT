"""子查询 7 节点流水线（Phase B）：单子查询的完整执行链路。

N1 要素补齐 → N2 NL2SQL 生成（失败重试）→ N3 软删除注入 → N4 执行（失败 LLM 修正）
→ N5 图表推荐（结果特征，确定性）→ N6 事实 + 异常（确定性）→ N7 溯源 + 解读（失败回退模板）。

每个节点均有明确输入/输出/失败模式/兜底/重试，任何失败不阻塞其余子查询（由编排器兜底）。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


class SubQueryNonSqlError(Exception):
    """N2 返回非 SQL 结果（clarify/knowledge/chat/refuse）：重试无意义。
    clarify 携带候选表（candidates），供上层做执行中澄清（needs_clarify）。"""

    def __init__(self, msg: str, candidates: list | None = None, intent: str = ""):
        super().__init__(msg)
        self.candidates = candidates or []
        self.intent = intent


@dataclass
class SubTaskResult:
    """单个子查询的执行结果（编排器 / 持久化 / 前端卡片共用）。"""
    sub_id: str = ""
    status: str = "success"        # success/error/cancelled
    title: str = ""
    chart_type: str = "bar"
    data: dict | None = None
    facts: dict = field(default_factory=dict)
    anomalies: list = field(default_factory=list)
    trace: dict = field(default_factory=dict)
    interpretation: str = ""
    sql: str = ""
    error: str = ""
    retryable: bool = True         # clarify 等非查询失败不可重试
    clarify_candidates: list = field(default_factory=list)  # 执行中澄清：候选表（[{table,comment}]）

    def to_card(self) -> dict:
        """转为 dashboard 卡片数据（落库 + 事件载荷）。"""
        return {
            "sub_id": self.sub_id,
            "title": self.title,
            "chart_type": self.chart_type,
            "data": self.data,
            "sql": self.sql,
            "facts": self.facts,
            "anomalies": self.anomalies,
            "trace": self.trace,
            "interpretation": self.interpretation,
            "status": self.status,
            "error": self.error,
            "retryable": self.retryable,
            "clarify_candidates": self.clarify_candidates,
        }


@dataclass
class SubTaskContext:
    """子查询流水线的运行上下文（由编排入口统一装配）。"""
    datasource_id: int
    workspace_id: int
    user_id: int
    dialect: str
    db: Any
    log: Any
    llm: Any | None
    history: list[dict] | None
    parent_spec: Any | None
    run_query_cached: Callable[..., dict]
    inject_soft_delete: Callable[..., str]
    emit: Callable[[str, dict], None]
    gen_retry: int = 1
    exec_retry: int = 1
    is_cancelled: Callable[[], bool] = lambda: False
    generate_sql: Callable[[Any], str] | None = None   # 可注入的 SQL 生成器（测试/定制用）


def _check_cancelled(ctx: SubTaskContext, stage: str) -> bool:
    if ctx.is_cancelled():
        logger.info("[子查询] %s 阶段收到取消信号", stage)
        return True
    return False


def run_subtask_pipeline(sub: Any, ctx: SubTaskContext) -> SubTaskResult:
    """执行单个子查询完整流水线（同步，运行于编排器线程池）。"""
    sub_id = sub.sub_id
    out = SubTaskResult(sub_id=sub_id, title=sub.title or sub.question[:30])

    # ---- N1 要素补齐（防御性：confirm/retry 传入的 spec 已是完整要素）----
    if _check_cancelled(ctx, "N1"):
        return SubTaskResult(sub_id=sub_id, status="cancelled")
    _ensure_sub_spec(sub, ctx)

    # ---- N2 NL2SQL 生成（失败重试 gen_retry 次）----
    try:
        if _check_cancelled(ctx, "N2"):
            return SubTaskResult(sub_id=sub_id, status="cancelled")
        ctx.emit("sub_progress", {"sub_id": sub_id, "stage": "generate",
                                  "msg": f"子查询 {sub_id}：正在生成 SQL…"})
        sql = _generate_sql_with_retry(sub, ctx)
        if not sql:
            raise RuntimeError("未生成 SQL（LLM 返回为空或 clarify）")
        out.sql = sql
    except SubQueryNonSqlError as exc:
        # 执行中澄清：clarify 且带候选表 → 不下发失败卡片，通知前端展示选表 UI，用户勾选后重跑
        if exc.intent == "clarify" and exc.candidates:
            logger.warning("[子查询][%s] N2 需要澄清选表（候选 %d 张），等待用户确认: %s",
                           sub_id, len(exc.candidates),
                           [c.get("table") for c in exc.candidates][:8])
            out.status = "needs_clarify"
            out.error = str(exc)
            out.clarify_candidates = exc.candidates
            ctx.emit("sub_clarify", {
                "sub_id": sub_id,
                "candidates": exc.candidates,
                "question": sub.question,
                "title": out.title,
            })
            return out
        logger.warning("[子查询][%s] N2 非查询意图（不可重试）: %s", sub_id, exc)
        return _fail(ctx, sub, out, f"无法生成 SQL：{exc}", retryable=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[子查询][%s] N2 生成失败: %s", sub_id, exc)
        return _fail(ctx, sub, out, f"SQL 生成失败: {exc}")

    # ---- N3 软删除注入 ----
    try:
        sql = ctx.inject_soft_delete(sql, ctx.datasource_id, sub.question, ctx.db)
        out.sql = sql
    except Exception as exc:  # noqa: BLE001
        logger.warning("[子查询][%s] N3 软删除注入失败（不阻断）: %s", sub_id, exc)
    ctx.emit("sub_sql", {"sub_id": sub_id, "sql": out.sql, "dialect": ctx.dialect})

    # ---- N4 执行（缓存复用 + 失败 LLM 自动修正 exec_retry 次）----
    try:
        if _check_cancelled(ctx, "N4"):
            return SubTaskResult(sub_id=sub_id, status="cancelled")
        ctx.emit("sub_progress", {"sub_id": sub_id, "stage": "execute",
                                  "msg": f"子查询 {sub_id}：正在执行…"})
        exec_result, sql_used = _execute_with_retry(sub, ctx, out.sql)
        out.sql = sql_used
        out.data = exec_result
    except Exception as exc:  # noqa: BLE001
        logger.warning("[子查询][%s] N4 执行失败: %s", sub_id, exc)
        return _fail(ctx, sub, out, f"执行失败: {exc}")

    # ---- N5 图表推荐（基于结果特征，确定性）----
    try:
        out.chart_type = _recommend_chart(sub, exec_result)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[子查询][%s] N5 图表推荐失败，默认 bar: %s", sub_id, exc)
        out.chart_type = "bar"

    # ---- N6 事实 + 异常（确定性计算，失败返回空不阻塞）----
    columns = (exec_result or {}).get("columns") or []
    rows = (exec_result or {}).get("rows") or []
    try:
        from .analyzer import compute_facts, detect_anomalies
        out.facts = compute_facts(sub.to_query_spec(), columns, rows) or {}
        out.anomalies = detect_anomalies(sub.to_query_spec(), columns, rows) or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("[子查询][%s] N6 事实/异常计算失败（忽略）: %s", sub_id, exc)
        out.facts = {"intent": sub.intent, "row_count": len(rows)}
        out.anomalies = []

    # ---- N7 溯源 + 解读（LLM 失败回退模板）----
    try:
        out.trace = _build_trace(sub, out.sql, exec_result, ctx)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[子查询][%s] N7 溯源失败（忽略）: %s", sub_id, exc)
        out.trace = {"sql": out.sql, "row_count": len(rows)}
    out.interpretation = _interpret(sub, exec_result, out.facts, ctx)

    ctx.emit("sub_result", {
        "sub_id": sub_id,
        "title": out.title,
        "chart_type": out.chart_type,
        "data": exec_result,
        "facts": out.facts,
        "anomalies": out.anomalies,
        "trace": out.trace,
        "interpretation": out.interpretation,
    })
    logger.info("[子查询][%s] 完成: chart=%s rows=%d", sub_id, out.chart_type, len(rows))
    return out


def _fail(ctx: SubTaskContext, sub: Any, out: SubTaskResult, msg: str,
          retryable: bool = True) -> SubTaskResult:
    out.status = "error"
    out.error = msg
    out.retryable = retryable
    ctx.emit("sub_error", {"sub_id": sub.sub_id, "error": msg,
                           "retryable": retryable, "stage": "pipeline"})
    return out


def _ensure_sub_spec(sub: Any, ctx: SubTaskContext) -> None:
    """N1：父级时间等上下文防御性继承（spec 缺失字段用父级补齐）。"""
    if not sub.time or not sub.time.expr:
        parent = ctx.parent_spec
        if parent is not None and getattr(parent, "time", None) and parent.time.expr:
            sub.time = parent.time
    if not sub.table_hints:
        parent = ctx.parent_spec
        if parent is not None and getattr(parent, "table_hints", None):
            sub.table_hints = list(parent.table_hints)
    if not sub.schema_name:
        parent = ctx.parent_spec
        if parent is not None and getattr(parent, "schema_name", ""):
            sub.schema_name = parent.schema_name


def _extract_sql_from_result(result: Any) -> tuple[str, str]:
    """从 generate_sql_stream 的 result 事件中提取 SQL 与 intent。

    兼容两种载荷形态：dict（{"sql": ..., "intent": ...}）与纯字符串
    （_mock_sql 等降级路径直接返回 SQL 文本）。返回 (sql, intent)。
    """
    if isinstance(result, dict):
        return str(result.get("sql") or ""), str(result.get("intent") or "")
    if isinstance(result, str):
        return result.strip(), ""
    return "", ""


def _extract_degraded_error(explain: str) -> str:
    """从降级文案中提取底层生成失败原因（形如「LLM 生成失败（原因）」）。"""
    m = re.search(r"生成失败（(.{1,120}?)）", explain)
    return m.group(1).strip() if m else ""


def _generate_sql_with_retry(sub: Any, ctx: SubTaskContext) -> str:
    """N2：流式生成 SQL；失败重试 gen_retry 次。优先使用注入的生成器（测试/定制）。"""
    if ctx.generate_sql is not None:
        max_attempts = 1 + max(0, ctx.gen_retry)
        last_error: str = ""
        for attempt in range(1, max_attempts + 1):
            try:
                sql = ctx.generate_sql(sub)
                if sql:
                    return sql
                last_error = "生成器返回空"
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
            if _check_cancelled(ctx, "N2"):
                return ""
        raise RuntimeError(last_error or "SQL 生成失败")

    from ..engine.nl2sql import generate_sql_stream

    question = sub.question.strip("，,。.；; ")
    # 用户澄清确认的表（Phase A 勾选）：拼入问题文本，走现有「已确认查询表」锁定路径
    confirmed = getattr(sub, "confirmed_tables", None) or []
    if confirmed:
        question = f"{question}，已确认查询表：{'、'.join(confirmed)}"
        logger.info("[子查询][%s] N2 使用澄清确认表: %s", sub.sub_id, confirmed)
    max_attempts = 1 + max(0, ctx.gen_retry)
    last_error: str = ""
    for attempt in range(1, max_attempts + 1):
        try:
            result = None
            for event in generate_sql_stream(
                ctx.datasource_id, ctx.workspace_id, question,
                history=ctx.history, llm=ctx.llm,
                schema_name=sub.schema_name or None,
                table_hints=sub.table_hints or None,
                spec_context=_spec_context(sub, ctx),
            ):
                if isinstance(event, dict) and event.get("type") == "result":
                    result = event.get("result")
            sql, intent = _extract_sql_from_result(result)
            if sql:
                # 防线：mock/降级 SQL（「前 100 行」应付式查询）不允许当作真实结果
                explain = result.get("explain") if isinstance(result, dict) else ""
                if isinstance(explain, str) and (
                        "[降级模式]" in explain or "演示模式" in explain):
                    # 区分两种降级来源，报错必须如实：
                    # - LLM 未配置/不可达（演示模式，或降级文案无「生成失败」）→ 指向模型配置；
                    # - LLM 已配置但 SQL 生成连续校验失败（[降级模式] LLM 生成失败（原因））
                    #   → 绝不归因于「模型未配置」，记入失败原因并让外层重试再给一次完整机会。
                    llm_unavailable = (ctx.llm is None
                                       or not getattr(ctx.llm, "configured", False))
                    if llm_unavailable or "生成失败" not in explain:
                        logger.warning("[子查询][%s] N2 拒绝 mock 降级 SQL（LLM 不可用）: %s",
                                       sub.sub_id, explain[:120])
                        raise SubQueryNonSqlError(
                            "LLM 不可用：系统未配置大模型或模型不可达。"
                            "请在「模型配置」中启用大模型后重试。",
                            intent="refuse")
                    last_error = _extract_degraded_error(explain) or "SQL 生成连续校验失败"
                    logger.warning(
                        "[子查询][%s] N2 SQL 生成降级（模型已配置，生成连续校验失败），外层重试: %s",
                        sub.sub_id, last_error[:120])
                    continue
                return sql
            # 非查询意图（clarify/knowledge/chat/refuse）：重试无意义，抛专用异常供上层标记不可重试
            if intent in ("clarify", "knowledge", "chat", "refuse"):
                explain = result.get("explain") if isinstance(result, dict) else ""
                candidates = (result.get("candidates") or []) if isinstance(result, dict) else []
                raise SubQueryNonSqlError(
                    f"{intent}: {explain or '该问题无法自动生成查询 SQL，请换种问法或补充表/字段信息'}",
                    candidates=candidates, intent=intent)
            last_error = intent or (f"result 为空（生成器返回 {type(result).__name__}）"
                                    if result is not None else "result 为空")
        except SubQueryNonSqlError:
            # 非查询意图（不可重试）：向上传播，由流水线 N2 捕获并标记 retryable=False
            raise
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
        logger.warning("[子查询][%s] N2 第 %d 次生成失败: %s",
                       sub.sub_id, attempt, last_error, exc_info=True)
        if _check_cancelled(ctx, "N2"):
            return ""
    raise RuntimeError(last_error or "SQL 生成失败")


def _spec_context(sub: Any, ctx: SubTaskContext) -> dict:
    """构造 spec_context：子查询结构化参数 + 父级 mapping/plan 上下文。"""
    try:
        return {"spec": sub.to_query_spec().model_dump(mode="json"),
                "mapping": {}, "plan": {}}
    except Exception:  # noqa: BLE001
        return {}


def _execute_with_retry(sub: Any, ctx: SubTaskContext, sql: str) -> tuple[dict, str]:
    """N4：执行（缓存复用）；失败时 LLM 携带 exec_error 自动修正 exec_retry 次。"""
    max_attempts = 1 + max(0, ctx.exec_retry)
    last_error: str = ""
    for attempt in range(1, max_attempts + 1):
        try:
            result = ctx.run_query_cached(sql, ctx.datasource_id, ctx.user_id,
                                          ctx.dialect, ctx.db, ctx.log)
            return result, sql
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            if attempt < max_attempts:
                logger.warning("[子查询][%s] N4 执行失败（第 %d/%d 次），自动修正: %s",
                               sub.sub_id, attempt, max_attempts, last_error[:200],
                               exc_info=True)
                ctx.emit("sub_progress", {"sub_id": sub.sub_id, "stage": "execute_retry",
                                          "msg": f"执行失败，自动重试（{attempt + 1}/{max_attempts}）…"})
                if ctx.llm is not None:
                    # LLM 修正：携带 exec_error 重新生成一次
                    from ..engine.nl2sql import generate_sql_stream
                    new_sql = ""
                    for event in generate_sql_stream(
                        ctx.datasource_id, ctx.workspace_id, sub.question,
                        history=ctx.history, llm=ctx.llm,
                        schema_name=sub.schema_name or None,
                        table_hints=sub.table_hints or None,
                        exec_error=last_error,
                        spec_context=_spec_context(sub, ctx),
                    ):
                        if isinstance(event, dict) and event.get("type") == "result":
                            new_sql, _ = _extract_sql_from_result(event.get("result"))
                    if new_sql:
                        sql = ctx.inject_soft_delete(new_sql, ctx.datasource_id,
                                                     sub.question, ctx.db)
                        continue
                # 无 LLM（或修正未产出）：原 SQL 重试（兜底 DB 瞬时错误）
                continue
    raise RuntimeError(last_error or "执行失败")


def _recommend_chart(sub: Any, exec_result: dict) -> str:
    """N5：图表推荐基于结果特征（确定性规则，不依赖 LLM）。

    优先级：用户问题显式图表关键词 > Phase A 提取的 chart_hint > 规则引擎。
    用户明确要求某图表时直接采用（数据形状不支持时降级到规则）。
    """
    from ..engine.chart_recommender import ChartRecommender

    columns = (exec_result or {}).get("columns") or []
    rows = (exec_result or {}).get("rows") or []
    metric_names = [m.name for m in (sub.metrics or [])]
    dim_names = [d.name for d in (sub.dimensions or [])]
    row_count = len(rows)

    # 1) 用户问题中显式要求的图表类型（最高优先级）
    user_pref = _detect_chart_keyword(sub.question or "")
    if user_pref and _chart_shape_supported(user_pref, row_count, columns):
        return user_pref

    # 2) Phase A 提取的 chart_hint（次高优先级）
    chart_hint = getattr(sub, "chart_hint", None)
    valid_hints = ("pie", "line", "bar", "rank", "kpi", "radar",
                   "combo", "stack_bar", "group_bar")
    if chart_hint and chart_hint in valid_hints and _chart_shape_supported(
            chart_hint, row_count, columns):
        return chart_hint

    # 3) 规则引擎推荐
    # spec 维度缺失时从结果列推断（首列非数值列视为维度），避免占比+饼图规则因维度为空失效
    if not dim_names and columns and rows:
        inferred = _infer_dimension_from_columns(columns, rows)
        if inferred:
            dim_names = [inferred]
    # 占比/构成语义提示（问题含 占比/比例/构成/百分比，或指标名含 占比，或 stat=ratio）
    _q = sub.question or ""
    ratio_hint = (any(w in _q for w in ("占比", "比例", "构成", "百分比", "份额", "比重"))
                  or any("占比" in (m or "") or "比例" in (m or "") for m in metric_names)
                  or getattr(getattr(sub, "action", None), "stat", None) == "ratio")
    recommender = ChartRecommender()
    chart_type = recommender.recommend(
        sub.intent, metric_names, dim_names,
        row_count=row_count, col_count=len(columns),
        ratio=ratio_hint)
    # 数据特征兜底修正：单行单指标 → KPI
    if row_count == 1 and len(columns) <= 2 and chart_type not in ("kpi",):
        chart_type = "kpi"
    return chart_type


# 用户问题中常见图表关键词 → 图表类型
_CHART_KEYWORD_MAP: dict[str, tuple[str, ...]] = {
    "pie": ("饼图", "饼状图", "扇形图"),
    "line": ("折线图", "趋势图", "曲线图", "面积图"),
    "bar": ("柱状图", "条形图", "柱形图"),
    "rank": ("排行榜", "排名图", "榜单"),
    "kpi": ("kpi", "指标卡", "数字卡", "大数字"),
    "radar": ("雷达图",),
    "combo": ("组合图", "双轴图", "双y轴"),
    "stack_bar": ("堆叠图", "堆叠柱状图"),
    "group_bar": ("分组柱状图", "分组图"),
}


def _detect_chart_keyword(question: str) -> str | None:
    """从用户问题中检测显式图表类型关键词（如「要求使用饼图展示」→ pie）。"""
    q = (question or "").lower()
    for chart, keywords in _CHART_KEYWORD_MAP.items():
        if any(kw in q for kw in keywords):
            return chart
    return None


def _chart_shape_supported(chart: str, row_count: int, columns: list) -> bool:
    """数据形状是否支持该图表类型（不支持时降级到规则推荐）。"""
    if chart == "pie":
        # 饼图至少需要 2 个分类，超过 8 个分类可读性差
        return 1 < row_count <= 8
    if chart == "kpi":
        return row_count <= 1
    if chart == "radar":
        return row_count <= 5 and len(columns) >= 2
    return True


def _infer_dimension_from_columns(columns: list, rows: list) -> str | None:
    """spec 维度缺失时，从结果列推断维度：首列非数值列视为分类维度。"""
    if not columns or not rows:
        return None
    first_col = str(columns[0])
    # 检查首列是否为非数值（取前 5 行采样）
    sample = [r[0] for r in rows[:5] if r]
    non_numeric = any(not _is_numeric(v) for v in sample)
    if non_numeric:
        return first_col
    return None


def _is_numeric(value: Any) -> bool:
    if value is None:
        return False
    try:
        float(value)
        return True
    except (ValueError, TypeError):
        return False


def _build_trace(sub: Any, sql: str, exec_result: dict, ctx: SubTaskContext) -> dict:
    """N7：口径溯源（表名 + SQL + 耗时 + 行数）。"""
    from ..engine.sql_trace import extract_sql_tables

    try:
        tables = extract_sql_tables(sql, ctx.dialect)
    except Exception:  # noqa: BLE001
        tables = []
    return {
        "tables": tables,
        "sql": sql,
        "dialect": ctx.dialect,
        "row_count": (exec_result or {}).get("row_count") or len((exec_result or {}).get("rows") or []),
        "latency_ms": (exec_result or {}).get("latency_ms"),
        "permission": (exec_result or {}).get("permission"),
    }


def _interpret_text_valid(text: str) -> bool:
    """N7 解读结果的轻量校验：非空、是业务结论而非拒答/代码/标记。"""
    if not text:
        return False
    low = text.strip()
    if low.startswith("```") or low.startswith("{") or low.startswith("["):
        return False
    bad = ("作为AI", "作为人工智能", "无法回答", "我不能", "Sorry", "I cannot")
    return not any(b in low for b in bad)


def _interpret(sub: Any, exec_result: dict, facts: dict, ctx: SubTaskContext) -> str:
    """N7：子查询解读。LLM 可用时生成一句解读（硬约束只引用 facts 数字）；
    首轮不合格 → 带诊断重试 1 次；仍失败/异常才回退确定性模板。"""
    if ctx.llm is None:
        return _template_interpret(sub, exec_result, facts)
    rows = (exec_result or {}).get("rows") or []
    base_prompt = (
        "你是数据解读助手。请基于给出的确定性事实，为子查询写一句 ≤60 字的中文业务结论。\n"
        "硬约束：\n"
        "1. 只能引用「事实」中出现的数字与名称，禁止编造、禁止四舍五入或换算出新数字；\n"
        "2. 不要复述问题，不要输出思考过程、Markdown、JSON、role 标记；\n"
        "3. 直接输出这一句结论本身。\n"
        f"问题：{sub.question}\n事实：{facts}\n返回行数：{len(rows)}"
    )
    text = ""
    for attempt in range(2):
        try:
            if attempt == 0:
                messages = [{"role": "user", "content": base_prompt}]
            else:
                diag = "上一次回答为空" if not text else "上一次回答不是一句业务结论（含拒答/代码/标记）"
                messages = [
                    {"role": "user", "content": base_prompt},
                    {"role": "assistant", "content": (text or "")[:300]},
                    {"role": "user", "content": (
                        f"【输出修正】{diag}。请只用「事实」中的数字，直接输出一句 ≤60 字中文结论，"
                        "不要复述问题、不要任何标记。")},
                ]
            raw = ctx.llm.chat(messages, temperature=0.2, thinking=False)
            text = (raw or "").strip()
            if _interpret_text_valid(text):
                return text[:200]
        except Exception as exc:  # noqa: BLE001
            logger.warning("[子查询][%s] N7 解读第 %d 次失败: %s", sub.sub_id, attempt + 1, exc)
    logger.warning("[子查询][%s] N7 解读两次均不合格，回退模板", sub.sub_id)
    return _template_interpret(sub, exec_result, facts)


def _template_interpret(sub: Any, exec_result: dict, facts: dict) -> str:
    """确定性模板解读（LLM 不可用/失败时的兜底，不产生新数字）。"""
    rows = (exec_result or {}).get("rows") or []
    row_count = (exec_result or {}).get("row_count") or len(rows)
    if not rows:
        return f"「{sub.question}」未查询到数据。"
    intent_label = {"trend": "趋势", "ranking": "排行", "compare": "对比",
                    "statistic": "统计", "detail": "明细"}.get(sub.intent, "查询")
    return f"「{sub.question}」共返回 {row_count} 行结果（{intent_label}）。"
