"""子查询 7 节点流水线（Phase B）：单子查询的完整执行链路。

N1 要素补齐 → N2 NL2SQL 生成（失败重试）→ N3 软删除注入 → N4 执行（失败 LLM 修正）
→ N5 图表推荐（结果特征，确定性）→ N6 事实 + 异常（确定性）→ N7 溯源 + 解读（失败回退模板）。

每个节点均有明确输入/输出/失败模式/兜底/重试，任何失败不阻塞其余子查询（由编排器兜底）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


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


def _fail(ctx: SubTaskContext, sub: Any, out: SubTaskResult, msg: str) -> SubTaskResult:
    out.status = "error"
    out.error = msg
    ctx.emit("sub_error", {"sub_id": sub.sub_id, "error": msg,
                           "retryable": True, "stage": "pipeline"})
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
                return sql
            last_error = intent or (f"result 为空（生成器返回 {type(result).__name__}）"
                                    if result is not None else "result 为空")
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
        logger.warning("[子查询][%s] N2 第 %d 次生成失败: %s", sub.sub_id, attempt, last_error)
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
                               sub.sub_id, attempt, max_attempts, last_error[:200])
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
    """N5：图表推荐基于结果特征（确定性规则，不依赖 LLM）。"""
    from ..engine.chart_recommender import ChartRecommender

    columns = (exec_result or {}).get("columns") or []
    rows = (exec_result or {}).get("rows") or []
    metric_names = [m.name for m in (sub.metrics or [])]
    dim_names = [d.name for d in (sub.dimensions or [])]
    recommender = ChartRecommender()
    chart_type = recommender.recommend(
        sub.intent, metric_names, dim_names,
        row_count=len(rows), col_count=len(columns))
    # 数据特征兜底修正：单行单指标 → KPI
    if len(rows) == 1 and len(columns) <= 2 and chart_type not in ("kpi",):
        chart_type = "kpi"
    return chart_type


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


def _interpret(sub: Any, exec_result: dict, facts: dict, ctx: SubTaskContext) -> str:
    """N7：子查询解读。LLM 可用时生成一句解读（硬约束只引用 facts 数字），失败回退模板。"""
    if ctx.llm is None:
        return _template_interpret(sub, exec_result, facts)
    try:
        rows = (exec_result or {}).get("rows") or []
        prompt = (
            "你是数据解读助手。请基于给出的确定性事实（只能引用其中的数字，不得编造）"
            "为子查询写一句 ≤60 字的中文结论，不要复述问题。\n"
            f"问题：{sub.question}\n事实：{facts}\n返回行数：{len(rows)}"
        )
        text = ctx.llm.chat([{"role": "user", "content": prompt}],
                            temperature=0.2, thinking=False)
        text = (text or "").strip()
        return text[:200] if text else _template_interpret(sub, exec_result, facts)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[子查询][%s] N7 解读失败，回退模板: %s", sub.sub_id, exc)
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
