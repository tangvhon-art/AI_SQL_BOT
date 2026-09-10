"""多查询问数处理（C11）。

被 chat.py 的 event_stream 调用：spec 解析完成后判断是否多查询，
是则走此模块完整处理（拆解→逐子查询SQL生成→执行→推送事件），否则返回 None 走旧链路。
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from ..engine.chart_recommender import ChartRecommender
from ..engine.multi_query import MultiQueryDecomposer
from ..engine.query_spec import MultiQuerySpec, SubQuerySpec, spec_to_dict

logger = logging.getLogger(__name__)


def try_handle_multi_query(
    question: str,
    spec: Any,
    datasource_id: int,
    workspace_id: int,
    user_id: int,
    dialect: str,
    llm: Any | None,
    db: Any,
    log: Any,
    conv_id: int,
    history: list[dict] | None,
    sse: Callable[[str, dict], str],
    run_query_cached: Callable[..., dict],
    inject_soft_delete: Callable[..., str],
) -> list[str] | None:
    """尝试多查询处理。

    返回 None 表示不是多查询，调用方应继续走旧链路。
    返回 list[str] 表示已处理，列表为 SSE 事件字符串（调用方 yield 后 return）。
    """
    events: list[str] = []

    # 1. 拆解
    logger.info("[多查询] 开始拆解: question=%r", question[:120])
    decomposer = MultiQueryDecomposer(llm=llm)
    multi_spec = decomposer.decompose(question, workspace_id, datasource_id)

    # 单查询兼容：只有 1 个子查询时走旧链路
    if multi_spec.is_single():
        logger.info("[多查询] 拆解结果为单查询（sub_count=%d, source=%s），走旧链路",
                    len(multi_spec.sub_queries), multi_spec.source)
        return None

    logger.info("[多查询] 拆解命中多查询: source=%s sub_count=%d subs=%s",
                multi_spec.source, len(multi_spec.sub_queries),
                [s.question[:50] for s in multi_spec.sub_queries])

    events.append(sse("progress", {"stage": "multi_query",
                                     "msg": f"检测到 {len(multi_spec.sub_queries)} 个子查询，正在拆解…"}))

    # 2. 为每个子查询填充 intent（复用现有 spec 的 intent 或默认 value）
    recommender = ChartRecommender()
    for sub in multi_spec.sub_queries:
        sub.intent = getattr(spec, "intent", "value") or "value"
        # 简单图表推荐（基于 intent）
        sub.chart_hint = recommender.recommend(sub.intent, sub.metrics, sub.dimensions)
        if not sub.title:
            sub.title = sub.question[:30]

    # 3. 推送 multi_spec
    multi_spec_dict = multi_spec.to_dict()
    events.append(sse("multi_spec", {
        "original_question": multi_spec.original_question,
        "sub_queries": multi_spec_dict["sub_queries"],
        "layout_hint": multi_spec.layout_hint,
        "source": multi_spec.source,
    }))

    # 4. 逐子查询生成 SQL + 执行
    from ..engine.nl2sql import generate_sql_stream

    dashboard_cards: list[dict] = []
    for sub in multi_spec.sub_queries:
        sub_id = sub.sub_id
        events.append(sse("progress", {"stage": "sub_sql", "sub_id": sub_id,
                                         "msg": f"子查询 {sub_id}：正在生成 SQL…"}))
        sql = None
        try:
            # 子查询走完整 NL2SQL 路径（不传 spec_context，避免 LLM 退化为翻译器）
            # 但传递父级 spec 的 table_hints（拆表检索词），帮助选表收窄范围，提高成功率
            sub_question = sub.question.strip("，,。.；; ")
            parent_hints = getattr(spec, "table_hints", None) or []
            logger.info("[多查询][%s] 调用 generate_sql_stream: question=%r table_hints=%s",
                        sub_id, sub_question[:60], parent_hints)
            result = None
            for event in generate_sql_stream(
                datasource_id, workspace_id, sub_question,
                history=history, llm=llm,
                schema_name=getattr(spec, "schema_name", "") or None,
                table_hints=parent_hints,
            ):
                if event["type"] == "result":
                    result = event["result"]
            if result and result.get("sql"):
                sql = result["sql"]
            else:
                raise RuntimeError(f"子查询 {sub_id} 未生成 SQL")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[多查询][%s] SQL 生成失败: %s", sub_id, exc)
            events.append(sse("sub_error", {"sub_id": sub_id, "error": f"SQL 生成失败: {exc}"}))
            dashboard_cards.append({"sub_id": sub_id, "title": sub.title,
                                     "chart_type": sub.chart_hint or "bar",
                                     "status": "error", "error": str(exc)})
            continue

        # 注入软删除
        sql = inject_soft_delete(sql, datasource_id, sub.question, db)
        events.append(sse("sub_sql", {"sub_id": sub_id, "sql": sql, "dialect": dialect}))

        # 执行
        events.append(sse("progress", {"stage": "sub_execute", "sub_id": sub_id,
                                         "msg": f"子查询 {sub_id}：正在执行…"}))
        try:
            exec_result = run_query_cached(sql, datasource_id, user_id, dialect, db, log)
            events.append(sse("sub_result", {
                "sub_id": sub_id,
                "title": sub.title,
                "chart_type": sub.chart_hint or "bar",
                "data": exec_result,
                "sql": sql,
            }))
            dashboard_cards.append({
                "sub_id": sub_id, "title": sub.title,
                "chart_type": sub.chart_hint or "bar",
                "data": exec_result, "sql": sql, "status": "success",
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning("[多查询][%s] 执行失败: %s", sub_id, exc)
            events.append(sse("sub_error", {"sub_id": sub_id, "error": f"执行失败: {exc}"}))
            dashboard_cards.append({"sub_id": sub_id, "title": sub.title,
                                     "chart_type": sub.chart_hint or "bar",
                                     "status": "error", "error": str(exc)})

    # 5. 推送 dashboard 汇总事件
    layout = recommender.recommend_layout(
        len(dashboard_cards),
        [c.get("chart_type", "bar") for c in dashboard_cards],
    )
    dashboard_payload = {
        "layout": layout,
        "cards": dashboard_cards,
        "original_question": question,
    }
    events.append(sse("dashboard", dashboard_payload))

    # 6. 保存 AI 回复消息（含 dashboard 完整数据，切换会话后可还原）
    try:
        import json as _json
        from ..models import ConversationMessage
        _content = {
            "mode": "dashboard",
            "dashboard": dashboard_payload,
            "multi_spec": multi_spec.to_dict(),
            "query_spec": spec_to_dict(spec),
        }
        _content_json = _json.loads(_json.dumps(_content, default=str))
        db.add(ConversationMessage(
            conversation_id=conv_id, role="assistant", content_type="result",
            content_json=_content_json))
        db.commit()
        logger.info("[多查询] 已保存 AI 回复消息: conv_id=%d cards=%d", conv_id, len(dashboard_cards))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[多查询] 保存 AI 回复消息失败: %s", exc)

    # 7. 记录日志
    try:
        log.multi_query_json = multi_spec.to_dict()
        log.generated_sql = "; ".join(c.get("sql", "") for c in dashboard_cards if c.get("sql"))
        db.commit()
    except Exception:
        pass

    events.append(sse("done", {}))
    return events
