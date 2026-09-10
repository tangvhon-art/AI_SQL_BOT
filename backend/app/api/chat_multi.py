"""多查询两阶段协同编排（C11 V2.0）。

Phase A（chat.py event_stream 内调用 try_handle_multi_query）：
    拆解 + 子查询要素补齐 → 推送 multi_spec（含 task_id，流程暂停）→ 保存 preview 消息 → 事件流结束。
    用户在前端预览面板确认/编辑/删除后，调用 confirm 接口进入 Phase B。

Phase B（confirm / retry 路由）：
    MultiQueryOrchestrator 并行执行子查询流水线（sub_task.run_subtask_pipeline），
    SSE 按 sub_id 实时推送（sub_progress/sub_sql/sub_result/sub_error），
    全部结束后推送 dashboard（布局 + 全卡最终态 + 总览解读）并落库。

配套接口：
    POST /chat/multi/confirm     确认执行（SSE 流）
    POST /chat/multi/regen       重新拆解（用户反馈）
    POST /chat/multi/{task_id}/cancel          整体取消
    POST /chat/multi/{message_id}/sub/{sub_id}/retry   单卡重试（SSE 流）
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from types import SimpleNamespace
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..engine.chart_recommender import ChartRecommender
from ..engine.llm_provider import resolve_llm_client, resolve_llm_client_by_id
from ..engine.multi_query import MultiQueryDecomposer, MultiQueryOrchestrator
from ..engine.query_spec import MultiQuerySpec, SubQuerySpec, spec_to_dict
from ..engine.sse_bus import sse_format
from ..engine.sub_task import SubTaskContext, SubTaskResult, run_subtask_pipeline
from .deps import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------- 编排器注册表（cancel 路由定位用） ----------
_ORCHESTRATORS: dict[str, MultiQueryOrchestrator] = {}
_ORCH_LOCK = threading.Lock()


def _register(task_id: str, orch: MultiQueryOrchestrator) -> None:
    with _ORCH_LOCK:
        _ORCHESTRATORS[task_id] = orch


def _unregister(task_id: str) -> None:
    with _ORCH_LOCK:
        _ORCHESTRATORS.pop(task_id, None)


def _get_orchestrator(task_id: str) -> MultiQueryOrchestrator | None:
    with _ORCH_LOCK:
        return _ORCHESTRATORS.get(task_id)


# ---------- 请求模型 ----------
class MultiConfirmIn(BaseModel):
    conversation_id: int | None = None
    question: str
    datasource_id: int | None = None
    workspace_id: int | None = None
    model_id: int | None = None
    task_id: str = ""                     # multi_spec 事件下发的任务标识（幂等）
    sub_queries: list[dict] = []          # 用户确认/编辑后的子查询清单（enabled 标记）


class RegenIn(BaseModel):
    question: str
    datasource_id: int | None = None
    workspace_id: int | None = None
    feedback: str = ""                    # 用户对上次拆解的反馈（可选）
    sub_queries: list[dict] = []          # 用户已编辑的子查询（参考）


class RetryIn(BaseModel):
    conversation_id: int | None = None
    workspace_id: int | None = None
    model_id: int | None = None
    datasource_id: int | None = None      # 优先于落库值（兼容历史消息未存数据源）


# =====================================================================
# Phase A：拆解 + 推送 multi_spec（流程暂停，等待用户确认）
# =====================================================================
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
    """Phase A：拆解 + 要素补齐 → 推送 multi_spec（流程暂停）。

    返回 None 表示单查询，调用方走旧链路；
    返回 list[str] 为 SSE 事件（multi_spec + done），调用方 yield 后结束事件流，
    等待用户在前端确认后调用 /chat/multi/confirm 进入 Phase B。
    """
    events: list[str] = []

    decomposer = MultiQueryDecomposer(llm=llm)
    multi_spec = decomposer.decompose(question, workspace_id, datasource_id,
                                      parent_spec=spec)

    # 单查询兼容：只有 1 个子查询时走旧链路
    if multi_spec.is_single():
        logger.info("[多查询] 拆解结果为单查询（sub_count=%d, source=%s），走旧链路",
                    len(multi_spec.sub_queries), multi_spec.source)
        return None

    logger.info("[多查询] Phase A 拆解命中: source=%s sub_count=%d task_id=%s",
                multi_spec.source, len(multi_spec.sub_queries), multi_spec.task_id)

    events.append(sse("progress", {"stage": "multi_query",
                                   "msg": f"检测到 {len(multi_spec.sub_queries)} 个子查询，已拆解，请确认后执行…"}))

    # 预览图表意向（Phase B 会基于结果特征重新推荐）
    recommender = ChartRecommender()
    for sub in multi_spec.sub_queries:
        if not sub.chart_hint:
            sub.chart_hint = recommender.recommend(
                sub.intent,
                [m.name for m in sub.metrics],
                [d.name for d in sub.dimensions])
        if not sub.title:
            sub.title = sub.question[:30]

    multi_spec_dict = multi_spec.to_dict()
    events.append(sse("multi_spec", {
        "task_id": multi_spec.task_id,
        "original_question": multi_spec.original_question,
        "sub_queries": multi_spec_dict["sub_queries"],
        "layout_hint": multi_spec.layout_hint,
        "source": multi_spec.source,
    }))

    # 保存 preview 消息（刷新会话可还原预览面板；confirm 后由 Phase B 覆盖为 dashboard）
    try:
        from ..models import ConversationMessage
        db.add(ConversationMessage(
            conversation_id=conv_id, role="assistant", content_type="multi_preview",
            content_json=_jsonable({
                "mode": "multi_preview",
                "task_id": multi_spec.task_id,
                "multi_spec": multi_spec_dict,
                "query_spec": spec_to_dict(spec) if spec else {},
            })))
        db.commit()
        logger.info("[多查询][%s] 已保存 multi_preview 消息: conv_id=%d",
                    multi_spec.task_id, conv_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[多查询][%s] 保存 preview 消息失败: %s", multi_spec.task_id, exc)

    # 记录日志
    try:
        log.multi_query_json = multi_spec_dict
        db.commit()
    except Exception:  # noqa: BLE001
        pass

    events.append(sse("done", {}))
    return events


# =====================================================================
# Phase B：confirm —— 并行执行子查询流水线（SSE 实时推送）
# =====================================================================
@router.post("/chat/multi/confirm")
def confirm(body: MultiConfirmIn, db: Session = Depends(get_db),
            user=Depends(get_current_user)):
    """确认执行：校验子查询清单 → 编排器并行执行 → SSE 实时推送 → 落库 dashboard。"""
    from ..config_override import get_effective as get_settings

    subs = body.sub_queries or []
    if not subs:
        raise HTTPException(status_code=400, detail="子查询清单为空")
    max_sub = get_settings().multi_max_subqueries
    if len(subs) > max_sub:
        raise HTTPException(status_code=400,
                            detail=f"子查询数量超过上限（{max_sub}）")
    enabled = [s for s in subs if s.get("enabled", True)]
    if not enabled:
        raise HTTPException(status_code=400, detail="至少需要一个启用的子查询")
    if not body.datasource_id:
        raise HTTPException(status_code=400, detail="缺少数据源")
    if not body.question:
        raise HTTPException(status_code=400, detail="缺少原始问题")

    sub_specs: list[SubQuerySpec] = []
    for item in enabled:
        try:
            sub_specs.append(SubQuerySpec.model_validate(item))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400,
                                detail=f"子查询 {item.get('sub_id', '?')} 字段非法: {exc}") from exc

    task_id = body.task_id or uuid.uuid4().hex[:12]
    logger.info("[多查询][%s] confirm 受理: %d 个子查询 → Phase B 并行执行",
                task_id, len(sub_specs))

    return StreamingResponse(
        _confirm_stream(body, sub_specs, task_id, db, user.id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def _confirm_stream(body: MultiConfirmIn, sub_specs: list[SubQuerySpec],
                          task_id: str, db: Session, user_id: int):
    """Phase B SSE 生成器：编排器在后台线程执行，事件经队列实时下发。"""
    queue: asyncio.Queue[str] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def emit(event: str, data: dict) -> None:
        queue.put_nowait(sse_format(event, data))

    def run() -> None:
        try:
            _run_confirm_sync(body, sub_specs, task_id, db, user_id, emit)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[多查询][%s] confirm 执行异常", task_id)
            emit("error", {"code": "INTERNAL", "msg": f"多查询执行失败: {exc}"})
        finally:
            emit("done", {})

    emit("multi_task", {"task_id": task_id})
    fut = loop.run_in_executor(None, run)

    while True:
        try:
            yield await asyncio.wait_for(queue.get(), timeout=0.5)
        except asyncio.TimeoutError:
            if fut.done():
                while not queue.empty():
                    yield queue.get_nowait()
                break


def _run_confirm_sync(body: MultiConfirmIn, sub_specs: list[SubQuerySpec],
                      task_id: str, db: Session, user_id: int,
                      emit: Callable[[str, dict], None]) -> None:
    """同步执行 Phase B：编排 → dashboard → 落库（运行于后台线程）。"""
    from ..config_override import get_effective as get_settings
    from ..models import ConversationMessage

    settings = get_settings()
    ws_id = body.workspace_id or 0

    llm = None
    if body.model_id:
        llm = resolve_llm_client_by_id(db, body.model_id, ws_id) or llm
    if llm is None:
        try:
            llm = resolve_llm_client(db, ws_id, scene="sql")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[多查询][%s] 获取 LLM 失败（子查询可用确定性兜底）: %s",
                           task_id, exc)

    dialect = _dialect_of(db, body.datasource_id)
    ctx = SubTaskContext(
        datasource_id=body.datasource_id,
        workspace_id=ws_id,
        user_id=user_id,
        dialect=dialect,
        db=db,
        log=_mock_log(ws_id),
        llm=llm,
        history=None,
        parent_spec=None,
        run_query_cached=_run_query_cached,
        inject_soft_delete=_inject_soft_delete,
        emit=emit,
        gen_retry=settings.multi_sub_gen_retry,
        exec_retry=settings.multi_sub_exec_retry,
        is_cancelled=lambda: _get_orchestrator(task_id).cancelled
        if _get_orchestrator(task_id) else False,
    )

    orch = MultiQueryOrchestrator(
        max_concurrent=settings.multi_max_concurrent,
        sub_timeout_s=settings.multi_sub_timeout_s)
    _register(task_id, orch)
    try:
        results = orch.run(sub_specs, ctx)
    finally:
        _unregister(task_id)

    # 汇总卡片（成功 / 失败 / 取消）
    cards: list[dict] = []
    for sub in sub_specs:
        r = results.get(sub.sub_id)
        if isinstance(r, SubTaskResult):
            card = r.to_card()
        elif isinstance(r, Exception):
            card = {"sub_id": sub.sub_id, "title": sub.title or sub.question[:30],
                    "chart_type": sub.chart_hint or "bar",
                    "status": "error", "error": str(r)}
        else:
            card = {"sub_id": sub.sub_id, "title": sub.title or sub.question[:30],
                    "chart_type": sub.chart_hint or "bar", "status": "cancelled"}
        card["sub_spec"] = sub.to_dict()   # 落库：单卡重试定位用
        cards.append(card)

    # 布局 + 总览
    recommender = ChartRecommender()
    layout = recommender.recommend_layout(
        len(cards), [c.get("chart_type", "bar") for c in cards])
    overview = _build_overview(body.question, cards, llm)

    dashboard_payload = {
        "layout": layout,
        "cards": cards,
        "overview": overview,
        "original_question": body.question,
        "datasource_id": body.datasource_id,   # 单卡重试定位数据源
    }

    # 保存 assistant 消息（dashboard 完整数据 + task_id + 确认后 spec，会话可还原）
    message_id = None
    try:
        _content = _jsonable({
            "mode": "dashboard",
            "dashboard": dashboard_payload,
            "multi_spec": {
                "task_id": task_id,
                "original_question": body.question,
                "sub_queries": [s.to_dict() for s in sub_specs],
            },
            "query_spec": {},
            "task_id": task_id,
        })
        msg = ConversationMessage(
            conversation_id=body.conversation_id or 0,
            role="assistant", content_type="result", content_json=_content)
        db.add(msg)
        db.commit()
        db.refresh(msg)
        message_id = msg.id
        logger.info("[多查询][%s] 已保存 dashboard 消息: id=%d cards=%d",
                    task_id, message_id, len(cards))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[多查询][%s] 保存 dashboard 消息失败: %s", task_id, exc)

    emit("dashboard", {**dashboard_payload, "message_id": message_id})


# =====================================================================
# 单卡重试：message_id + sub_id 定位，重跑该子查询流水线
# =====================================================================
@router.post("/chat/multi/{message_id}/sub/{sub_id}/retry")
def sub_retry(message_id: int, sub_id: str, body: RetryIn,
              db: Session = Depends(get_db), user=Depends(get_current_user)):
    from ..models import ConversationMessage

    msg = db.query(ConversationMessage).get(message_id)
    if not msg:
        raise HTTPException(status_code=404, detail="消息不存在")
    content = msg.content_json or {}
    cards = (content.get("dashboard") or {}).get("cards", [])
    card = next((c for c in cards if c.get("sub_id") == sub_id), None)
    if not card:
        raise HTTPException(status_code=404, detail="卡片不存在")
    sub_spec_dict = card.get("sub_spec")
    if not sub_spec_dict:
        raise HTTPException(status_code=400, detail="该卡片缺少子查询规格，无法重试")
    try:
        sub = SubQuerySpec.model_validate(sub_spec_dict)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"子查询规格非法: {exc}") from exc

    ws_id = body.workspace_id or (content.get("multi_spec") or {}).get("workspace_id") or 0
    datasource_id = (body.datasource_id
                     or (content.get("dashboard") or {}).get("datasource_id") or 0)

    logger.info("[多查询][重试] message_id=%d sub_id=%s 重新执行", message_id, sub_id)
    return StreamingResponse(
        _retry_stream(msg, sub, card, db, user.id, ws_id, body.model_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def _retry_stream(msg: Any, sub: SubQuerySpec, card: dict,
                        db: Session, user_id: int, ws_id: int, model_id: int | None):
    queue: asyncio.Queue[str] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def emit(event: str, data: dict) -> None:
        queue.put_nowait(sse_format(event, data))

    def run() -> None:
        try:
            _run_retry_sync(msg, sub, card, db, user_id, ws_id, model_id, emit)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[多查询][重试] sub=%s 执行异常", sub.sub_id)
            emit("sub_error", {"sub_id": sub.sub_id, "error": str(exc),
                               "retryable": True, "stage": "retry"})
        finally:
            emit("done", {})

    fut = loop.run_in_executor(None, run)
    while True:
        try:
            yield await asyncio.wait_for(queue.get(), timeout=0.5)
        except asyncio.TimeoutError:
            if fut.done():
                while not queue.empty():
                    yield queue.get_nowait()
                break


def _run_retry_sync(msg: Any, sub: SubQuerySpec, card: dict, db: Session,
                    user_id: int, ws_id: int, model_id: int | None,
                    emit: Callable[[str, dict], None]) -> None:
    from ..config_override import get_effective as get_settings

    settings = get_settings()
    content = msg.content_json or {}
    dashboard = content.get("dashboard") or {}
    datasource_id = dashboard.get("datasource_id") or 0
    question = dashboard.get("original_question") or sub.question

    llm = None
    if model_id:
        llm = resolve_llm_client_by_id(db, model_id, ws_id)
    if llm is None:
        try:
            llm = resolve_llm_client(db, ws_id, scene="sql")
        except Exception:  # noqa: BLE001
            llm = None

    ctx = SubTaskContext(
        datasource_id=datasource_id,
        workspace_id=ws_id,
        user_id=user_id,
        dialect=_dialect_of(db, datasource_id),
        db=db,
        log=_mock_log(ws_id),
        llm=llm,
        history=None,
        parent_spec=None,
        run_query_cached=_run_query_cached,
        inject_soft_delete=_inject_soft_delete,
        emit=emit,
        gen_retry=settings.multi_sub_gen_retry,
        exec_retry=settings.multi_sub_exec_retry,
    )

    result = run_subtask_pipeline(sub, ctx)

    # 更新消息内该卡片状态（会话恢复 / 前端刷新一致性）
    try:
        cards = dashboard.get("cards") or []
        for c in cards:
            if c.get("sub_id") == sub.sub_id:
                c.update(result.to_card())
                c["sub_spec"] = sub.to_dict()
                break
        dashboard["cards"] = cards
        content["dashboard"] = dashboard
        msg.content_json = content
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[多查询][重试] 更新消息失败: %s", exc)

    emit("dashboard", {"layout": dashboard.get("layout", []),
                       "cards": cards, "original_question": question,
                       "message_id": msg.id, "retry_sub_id": sub.sub_id})


# =====================================================================
# 重新拆解（用户反馈）与整体取消
# =====================================================================
@router.post("/chat/multi/regen")
def regen(body: RegenIn, db: Session = Depends(get_db),
          user=Depends(get_current_user)):
    """重新拆解：返回新的子查询清单（前端刷新预览面板）。"""
    llm = None
    try:
        llm = resolve_llm_client(db, body.workspace_id or 0, scene="sql")
    except Exception:  # noqa: BLE001
        llm = None
    decomposer = MultiQueryDecomposer(llm=llm)
    multi_spec = decomposer.decompose(
        body.question, body.workspace_id or 0, body.datasource_id or 0)

    recommender = ChartRecommender()
    for sub in multi_spec.sub_queries:
        if not sub.chart_hint:
            sub.chart_hint = recommender.recommend(
                sub.intent, [m.name for m in sub.metrics],
                [d.name for d in sub.dimensions])
        if not sub.title:
            sub.title = sub.question[:30]

    return {"task_id": multi_spec.task_id,
            "sub_queries": [s.to_dict() for s in multi_spec.sub_queries],
            "layout_hint": multi_spec.layout_hint,
            "source": multi_spec.source}


@router.post("/chat/multi/{task_id}/cancel")
def cancel(task_id: str, user=Depends(get_current_user)):
    """整体取消：未完成子查询置 cancelled，已完成卡片保留。"""
    orch = _get_orchestrator(task_id)
    if not orch:
        return {"cancelled": False, "msg": "任务不存在或已完成"}
    orch.cancel()
    return {"cancelled": True}


# =====================================================================
# 内部工具
# =====================================================================
def _dialect_of(db: Session, datasource_id: int) -> str:
    try:
        from ..models import Datasource
        ds = db.query(Datasource).get(datasource_id)
        return ds.type if ds else "mysql"
    except Exception:  # noqa: BLE001
        return "mysql"


def _mock_log(workspace_id: int) -> SimpleNamespace:
    """run_query_cached 需要的轻量 log 代理（confirm 阶段无 QueryLog 实体）。"""
    return SimpleNamespace(workspace_id=workspace_id, cache_hit=False,
                           cache_key=None, id=0)


def _run_query_cached(sql: str, datasource_id: int, user_id: int,
                      dialect: str, db: Session, log: Any) -> dict:
    """执行并读写结果缓存（与 chat.py._run_query_cached 同逻辑，独立实现避免循环依赖）。"""
    from ..config_override import get_effective as get_settings
    from ..engine.cache_manager import (fingerprint_sql, lookup_result_cache,
                                        store_result_cache)
    from ..executor import run_query

    settings = get_settings()
    if not settings.cache_enabled:
        return run_query(datasource_id, sql, user_id, dialect=dialect)
    cache_key = fingerprint_sql(sql, dialect)
    cached = lookup_result_cache(cache_key, log.workspace_id, datasource_id, db)
    if cached is not None:
        log.cache_hit = True
        log.cache_key = cache_key
        return cached
    result = run_query(datasource_id, sql, user_id, dialect=dialect)
    store_result_cache(cache_key, sql, log.workspace_id, datasource_id, result, db)
    return result


def _inject_soft_delete(sql: str, datasource_id: int, question: str, db: Session) -> str:
    from ..engine.soft_delete import inject_soft_delete
    return inject_soft_delete(sql, datasource_id, question, db)


def _build_overview(question: str, cards: list[dict], llm: Any) -> str:
    """总览解读：LLM 生成（只引用卡片 interpretation/facts），失败回退确定性模板。"""
    ok_cards = [c for c in cards if c.get("status") == "success"]
    fail_count = len(cards) - len(ok_cards)
    if not ok_cards:
        return (f"共 {len(cards)} 个子查询均未成功执行"
                + (f"，{fail_count} 个失败" if fail_count else "。"))
    try:
        if llm is not None:
            lines = []
            for c in ok_cards[:8]:
                interp = (c.get("interpretation") or "").strip()
                lines.append(f"- {c.get('title')}：{interp or '已返回数据'}")
            prompt = (
                "你是数据分析总览助手。请基于以下各子查询的结论（只能引用其中的数字，"
                "不得编造），用 ≤80 字的中文总结整体数据情况，不要复述问题。\n"
                f"问题：{question}\n子查询结论：\n" + "\n".join(lines))
            text = (llm.chat([{"role": "user", "content": prompt}],
                             temperature=0.2, thinking=False) or "").strip()
            if text:
                return text[:300]
    except Exception as exc:  # noqa: BLE001
        logger.warning("[多查询] 总览 LLM 生成失败，回退模板: %s", exc)
    suffix = f"；{fail_count} 个子查询失败" if fail_count else ""
    return f"共 {len(ok_cards)} 个子查询查询成功" + suffix + "。"


def _jsonable(obj: Any) -> Any:
    """递归转换为 JSON 可序列化结构。"""
    return json.loads(json.dumps(obj, ensure_ascii=False, default=str))
