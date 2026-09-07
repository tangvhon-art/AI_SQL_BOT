"""AI 问数：SSE 流式对话（五层流水线：preprocess→intent→spec→map→plan→execute→chart→summary→trace→done）、
会话历史、结果保存复用、反馈。

重构要点（对应概设方案）：
- L2 意图理解层产出 QuerySpec，SSE 新增 spec 事件（前端展示"我理解的问题"）
- L3 分析规则引擎确定性计算，模板无法覆盖时走 NL2SQL LLM 兜底
- L4 四层结论（核心数据/变化/亮点异常/总结），数字 100% 来自确定性计算
- L5 SSE 新增 clarify/trace 事件 + 会话记忆（上一轮 QuerySpec 驱动指代消解）
"""
import json
import logging
import re
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db, session_scope
from ..engine.analyzer import (AnalysisError, build_analysis_sql,
                               compute_compare_facts, compute_facts,
                               detect_anomalies)
from ..engine.chart import build_chart_option
from ..engine.intent import detect_intent, parse_query_spec
from ..engine.llm_provider import resolve_llm_client, resolve_llm_client_by_id
from ..engine.mapping import MappingError, PermissionMappingError, map_spec_to_schema
from ..engine.nl2sql import generate_sql_stream
from ..engine.preprocess import normalize_question
from ..engine.query_spec import (INTENT_LABELS, load_intent_dicts,
                                 spec_from_dict, spec_to_dict)
from ..engine.soft_delete import inject_soft_delete
from ..executor import run_query, to_jsonable
from ..llm import LLMClient, LLMError
from ..models import (Conversation, ConversationMessage, FaqPair, QueryLog,
                      SavedQuery)
from .deps import get_current_user

# 文件问答
from ..engine.doc_chat import doc_chat_stream

router = APIRouter(tags=["chat"])
logger = logging.getLogger(__name__)


class ChatIn(BaseModel):
    conversation_id: int | None = None
    question: str
    datasource_id: int | None = None
    workspace_id: int | None = None
    schema_name: str | None = None  # 前端显式指定查询范围 schema（项目/库），可选
    clarify_answer: dict | None = None  # 澄清点选结果（指标/维度/时间）
    model_id: int | None = None  # 前端选择的大模型 ID；不传则使用工作空间默认模型
    file_ids: list[str] | None = None  # 文件问答：上传文件 ID 列表，非空则走文件问答链路（不走意图识别/SQL）
    doc_session_id: str | None = None  # 文件问答：前端生成的会话标识，用于从内存 DocStore 取文件


class FeedbackIn(BaseModel):
    feedback: str  # good / bad
    note: str = ""


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _load_history(db: Session, conv_id: int) -> list[dict]:
    rows = (db.query(ConversationMessage)
            .filter(ConversationMessage.conversation_id == conv_id)
            .order_by(ConversationMessage.id.desc()).limit(8).all())
    return [{"role": m.role,
             "text": (m.content_json or {}).get("text", ""),
             "sql": (m.content_json or {}).get("sql", "")}
            for m in reversed(rows)]


def _load_prev_spec(db: Session, conv_id: int):
    """会话记忆：读取最近一轮结果的 QuerySpec，供省略式追问继承。"""
    if not conv_id:
        return None
    last = (db.query(ConversationMessage)
            .filter(ConversationMessage.conversation_id == conv_id,
                    ConversationMessage.content_type == "result")
            .order_by(ConversationMessage.id.desc()).first())
    if not last:
        return None
    return spec_from_dict((last.content_json or {}).get("query_spec"))


def _doc_chat_event_stream(body: ChatIn, db: Session, user, ws_id: int):
    """文件问答 SSE 生成器：创建会话 → 调用 doc_chat_stream → 包装事件 → 保存消息。"""
    conv_id = body.conversation_id
    history: list[dict] = []
    if conv_id:
        history = _load_history(db, conv_id)
    else:
        conv = Conversation(workspace_id=ws_id, user_id=user.id, title=body.question[:40])
        db.add(conv)
        db.commit()
        db.refresh(conv)
        conv_id = conv.id

    # 保存用户消息
    db.add(ConversationMessage(conversation_id=conv_id, role="user",
                               content_type="text",
                               content_json={"text": body.question}))
    db.commit()

    log = QueryLog(workspace_id=ws_id, user_id=user.id, conversation_id=conv_id,
                   question=body.question, intent="doc_chat")
    db.add(log)
    db.commit()

    # doc_session_id：优先前端传入，否则用 conv_id 构造
    doc_session_id = body.doc_session_id or f"doc_conv_{conv_id}"
    full_answer = ""
    references: list[dict] = []
    trace_data: dict = {}
    error_occurred = False

    try:
        yield _sse("conv_id", {"conversation_id": conv_id})

        for evt in doc_chat_stream(
            question=body.question,
            file_ids=body.file_ids or [],
            session_id=doc_session_id,
            workspace_id=ws_id,
            model_id=body.model_id,
            history=history,
        ):
            event_name = evt.get("event", "")
            data = evt.get("data", {})
            if event_name == "answer":
                full_answer += data.get("delta", "")
            elif event_name == "references":
                references = data.get("references", [])
            elif event_name == "trace":
                trace_data = data
            elif event_name == "error":
                error_occurred = True
            yield _sse(event_name, data)

    except Exception as exc:  # noqa: BLE001
        logger.exception("文件问答链路异常")
        error_occurred = True
        yield _sse("error", {"code": "INTERNAL", "msg": str(exc)})
        yield _sse("done", {})

    # 保存 AI 回答消息
    try:
        db.add(ConversationMessage(
            conversation_id=conv_id, role="assistant", content_type="result",
            content_json={
                "text": full_answer,
                "references": references,
                "trace": trace_data,
                "mode": "doc_chat",
                "file_ids": body.file_ids or [],
            }))
        log.status = "failed" if error_occurred else "success"
        log.answer_text = full_answer[:2000]
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("文件问答消息保存失败: %s", exc)


@router.post("/chat")
def chat(body: ChatIn, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """SSE 问数入口：逐步下发事件，结果落库（会话消息 + 审计）。"""
    ws_id = body.workspace_id or user.workspace_id

    # ===== 文件问答分支：有附件时走独立链路（不走意图识别/SQL） =====
    if body.file_ids:
        return StreamingResponse(
            _doc_chat_event_stream(body, db, user, ws_id),
            media_type="text/event-stream",
        )

    def event_stream():
        conv_id = body.conversation_id
        history = []
        prev_spec = None
        if conv_id:
            history = _load_history(db, conv_id)
            prev_spec = _load_prev_spec(db, conv_id)
        else:
            conv = Conversation(workspace_id=ws_id, user_id=user.id, title=body.question[:40])
            db.add(conv)
            db.commit()
            db.refresh(conv)
            conv_id = conv.id

        db.add(ConversationMessage(conversation_id=conv_id, role="user",
                                   content_type="text",
                                   content_json={"text": body.question}))
        db.commit()

        log = QueryLog(workspace_id=ws_id, user_id=user.id, conversation_id=conv_id,
                       question=body.question, intent="nl2sql")
        db.add(log)
        db.commit()
        db.refresh(log)

        start = time.time()
        try:
            yield _sse("conv_id", {"conversation_id": conv_id})

            # ===== L1 预处理 =====
            yield _sse("progress", {"stage": "preprocess", "msg": "正在理解问题…"})
            dicts = load_intent_dicts(db, ws_id)
            dict_terms = [d["term"] for lst in dicts.values() for d in lst]
            question, inherited = normalize_question(
                body.question,
                prev_spec.model_dump(mode="json") if prev_spec else None,
                dict_terms)

            # V1.1 表澄清回填：「已确认查询表：X」是前端点选后的系统回填句，
            # 不是新业务问题——从上一轮 clarify 消息还原 original_question，确认表经 history 锁定选表
            is_table_confirm = bool(re.search(r"已确认查询表[:：]", body.question or ""))
            if is_table_confirm:
                _last_clarify = (db.query(ConversationMessage)
                                 .filter(ConversationMessage.conversation_id == conv_id,
                                         ConversationMessage.content_type == "clarify")
                                 .order_by(ConversationMessage.id.desc()).first())
                _orig = (_last_clarify.content_json or {}).get("original_question") if _last_clarify else None
                if _orig:
                    question, inherited = normalize_question(
                        _orig,
                        prev_spec.model_dump(mode="json") if prev_spec else None,
                        dict_terms)
                    logger.info("表澄清回填，还原原始问题: %s", question)

            # ===== FAQ 优先匹配（意图识别之前）：命中即用 FAQ 对回答 =====
            yield _sse("progress", {"stage": "faq", "msg": "正在匹配知识库 FAQ…"})
            faq, _faq_hit = _match_faq_answer(db, ws_id, question)
            if faq is not None:
                answer = (faq.answer or "").strip() or f"Q：{faq.question}\nA：（该 FAQ 未填写答案）"
                yield _sse("summary", {"text": answer, "kind": "faq"})
                db.add(ConversationMessage(
                    conversation_id=conv_id, role="assistant", content_type="result",
                    content_json={"text": answer, "kind": "faq"}))
                log.intent = "faq"
                log.executed = False
                log.latency_ms = int((time.time() - start) * 1000)
                db.commit()
                yield _sse("done", {})
                return

            # 数据源解析
            datasource_id = body.datasource_id
            if not datasource_id:
                from ..models import Datasource
                ds = (db.query(Datasource)
                      .filter(Datasource.workspace_id == ws_id,
                              Datasource.status == "ok")
                      .order_by(Datasource.id).first())
                datasource_id = ds.id if ds else None
            if not datasource_id:
                yield _sse("error", {"code": "NO_DATASOURCE",
                                     "msg": "请先在「数据源」中配置并采集 Schema"})
                log.intent = "no_datasource"
                log.executed = False
                log.latency_ms = int((time.time() - start) * 1000)
                db.commit()
                yield _sse("done", {})
                return

            # LLM 模型：前端指定 model_id 则按 ID 加载，否则使用工作空间默认模型（scene=sql）
            if body.model_id:
                llm = resolve_llm_client_by_id(db, body.model_id, ws_id) or resolve_llm_client(db, ws_id, scene="sql")
            else:
                llm = resolve_llm_client(db, ws_id, scene="sql")

            # ===== 意图分诊（chat / knowledge / data）=====
            yield _sse("progress", {"stage": "intent", "msg": "正在识别意图…"})
            intent = detect_intent(body.question, history, llm)
            log.intent = intent
            _triage_labels = {"chat": "闲聊", "knowledge": "知识问答", "data": "数据问数"}
            yield _sse("progress", {"stage": "intent", "msg": f"意图：{_triage_labels.get(intent, intent)}"})

            if intent == "chat":
                yield from _stream_chat_answer(body.question, history, llm, conv_id, db, log, start)
                return

            if intent == "knowledge":
                yield from _stream_knowledge_answer(body.question, history, llm, conv_id, db, log, start, ws_id)
                return

            # ===== L2 意图理解：QuerySpec 结构化参数 =====
            yield _sse("progress", {"stage": "spec", "msg": "正在解析查询要素（指标/维度/时间/条件）…"})
            parse_result = parse_query_spec(
                question, datasource_id, ws_id, llm=llm,
                prev_spec=prev_spec, clarify_answer=body.clarify_answer,
                dicts=dicts, schema_name=body.schema_name)
            if parse_result["status"] == "refuse":
                yield _sse("summary", {"text": parse_result["message"]})
                log.executed = False
                db.commit()
                yield _sse("done", {})
                return
            if parse_result["status"] == "clarify":
                text = parse_result["message"]
                candidates = parse_result.get("candidates") or []
                missing = parse_result.get("missing") or []
                yield _sse("summary", {"text": text, "candidates": candidates,
                                        "kind": "spec", "original_question": body.question,
                                        "missing": missing})
                db.add(ConversationMessage(
                    conversation_id=conv_id, role="assistant", content_type="clarify",
                    content_json={"text": text, "candidates": candidates, "kind": "spec",
                                  "original_question": body.question, "missing": missing}))
                log.clarify_count = (log.clarify_count or 0) + 1
                log.executed = False
                db.commit()
                yield _sse("done", {})
                return

            spec = parse_result["spec"]
            spec.original_question = body.question
            spec.inherited_from.update(inherited or {})
            log.spec_json = spec_to_dict(spec)
            log.intent = spec.intent
            db.commit()
            yield _sse("spec", spec_to_dict(spec))
            yield _sse("progress", {"stage": "map",
                                    "msg": f"意图：{INTENT_LABELS.get(spec.intent, spec.intent)}"})

            # ===== V1.1 Step6 前置：相近表≥2 且问题无明确限定词 → 先澄清（模板/LLM 路径统一拦截）=====
            # 用户已点选确认表的回填轮不再重复澄清
            from ..engine.nl2sql import _detect_clarify
            table_clarify = None if is_table_confirm else _detect_clarify(question, datasource_id)
            if table_clarify:
                cand_text = "；".join(f"{c['table']}（{c['comment'] or '无注释'}）"
                                      for c in table_clarify)
                yield _sse("summary", {
                    "text": f"您的问题对应多张表，请勾选需要查询的表（可多选，将分别查询）：{cand_text}",
                    "candidates": table_clarify, "original_question": body.question})
                db.add(ConversationMessage(
                    conversation_id=conv_id, role="assistant", content_type="clarify",
                    content_json={"text": f"您的问题对应多张表，请勾选：{cand_text}",
                                  "candidates": table_clarify,
                                  "original_question": body.question}))
                log.clarify_count = (log.clarify_count or 0) + 1
                log.executed = False
                db.commit()
                yield _sse("done", {})
                return

            # ===== L3 数据源三层映射 =====
            # MappingError（如"使用率"等计算指标无直接字段）不再直接报错，
            # 降级到 LLM 全表选表路径，由 LLM 根据问题语义选表并生成计算 SQL。
            mapping = None
            try:
                mapping = map_spec_to_schema(spec, datasource_id, user.id, dicts=dicts)
            except PermissionMappingError as exc:
                yield _sse("error", {"code": "PERMISSION", "msg": str(exc)})
                log.executed = False
                db.commit()
                yield _sse("done", {})
                return
            except MappingError as exc:
                logger.info("映射失败降级 LLM：%s", exc)
                mapping = None
            if mapping is not None:
                log.mapping_json = {k: v for k, v in mapping.items() if k != "table"}
            db.commit()

            # ===== L3 分析规则引擎（确定性 SQL 模板）或 LLM 全表选表 =====
            from ..models import Datasource
            ds = db.query(Datasource).get(datasource_id)
            dialect = ds.type if ds else "mysql"
            yield _sse("progress", {"stage": "plan", "msg": "正在生成查询逻辑…"})
            llm_selected_tables = None
            llm_select_source = None
            used_template = False
            sql = None
            analysis_meta = {}
            inner = "query"
            if mapping is not None and not is_table_confirm:
                try:
                    analysis = build_analysis_sql(spec, mapping, dialect)
                    sql = analysis["sql"]
                    analysis_meta = analysis["analysis_meta"]
                    used_template = True
                except AnalysisError as exc:
                    logger.info("模板降级 LLM：%s", exc)
            elif is_table_confirm:
                logger.info("用户已确认表，跳过模板 SQL，强制走 LLM 路径（综合使用确认表生成 SQL）")

            if not used_template:
                # NL2SQL LLM 路径：全表选表→字段→关系→生成 SQL（映射失败/模板无法覆盖均走此路）
                spec_context = {"spec": spec_to_dict(spec)}
                if mapping is not None:
                    spec_context["mapping"] = {k: v for k, v in mapping.items() if k != "table"}
                yield _sse("progress", {"stage": "generate", "msg": "正在生成 SQL…"})
                result = None
                history = _load_history(db, conv_id)  # 含本轮确认句，供 confirmed 选表锁定
                for event in generate_sql_stream(datasource_id, ws_id, question,
                                                  history=history, llm=llm,
                                                  spec_context=spec_context,
                                                  schema_name=body.schema_name or spec.schema):
                    if event["type"] == "stream":
                        yield _sse("stream", {"delta": event["delta"]})
                    elif event["type"] == "retry":
                        yield _sse("progress", {"stage": "generate", "msg": event["msg"]})
                    elif event["type"] == "result":
                        result = event["result"]
                if not result or not result.get("sql"):
                    reason = (result or {}).get("explain") or "无法生成 SQL"
                    yield _sse("summary", {"text": f"无法回答：{reason}"})
                    log.executed = False
                    log.fallback = True
                    db.commit()
                    yield _sse("done", {})
                    return
                inner = result.get("intent") or "query"
                if inner == "chat":
                    yield _sse("summary", {"text": result.get("explain") or "你好！"})
                    log.executed = False
                    db.commit()
                    yield _sse("done", {})
                    return
                if inner == "clarify":
                    candidates = result.get("candidates") or []
                    text = result.get("explain") or "您的问题对应多张表，请勾选需要查询的表"
                    yield _sse("summary", {"text": text, "candidates": candidates,
                                            "original_question": body.question})
                    db.add(ConversationMessage(
                        conversation_id=conv_id, role="assistant", content_type="clarify",
                        content_json={"text": text, "candidates": candidates,
                                      "original_question": body.question}))
                    log.executed = False
                    db.commit()
                    yield _sse("done", {})
                    return
                if inner == "knowledge":
                    yield from _finish_knowledge(body.question, result.get("rag_hits") or {},
                                                 llm, conv_id, db, log, start)
                    return
                sql = result["sql"]
                llm_selected_tables = result.get("selected_tables") or [
                    {"id": None, "table_name": t, "comment": ""} for t in (result.get("tables") or [])]
                llm_select_source = result.get("select_source") or "llm"
                analysis_meta = {"intent": spec.intent}

            # ===== 自动注入 is_deleted = 0 =====
            sql = inject_soft_delete(sql, datasource_id, body.question, db)

            log.generated_sql = sql
            log.matched_tables = ",".join(m["table"] for m in (mapping or {}).get("metrics", []))
            db.commit()
            yield _sse("sql", {"sql": sql, "dialect": dialect, "permission": "待校验"})

            # ===== 执行 =====
            yield _sse("progress", {"stage": "execute", "msg": "正在查询数据…"})
            exec_retried = False
            try:
                exec_result = run_query(datasource_id, sql, user.id, dialect=dialect)
            except Exception as exc:  # noqa: BLE001
                logger.warning("执行失败: %s", exc)
                # LLM 路径执行失败 → 回传错误让 LLM 自动修正一次（模板路径失败直接报错）
                if (inner == "query" and not used_template
                        and llm is not None and llm.configured and not exec_retried):
                    exec_retried = True
                    yield _sse("progress", {"stage": "execute",
                                            "msg": f"执行失败（{str(exc)[:60]}），正在自动修正…"})
                    result2 = None
                    for event in generate_sql_stream(datasource_id, ws_id, question,
                                                     history=history, llm=llm,
                                                     spec_context=spec_context,
                                                     schema_name=body.schema_name or spec.schema,
                                                     exec_error=str(exc)):
                        if event["type"] == "result":
                            result2 = event["result"]
                    if result2 and result2.get("sql") and result2.get("intent") in (None, "query"):
                        sql = result2["sql"]
                        sql = inject_soft_delete(sql, datasource_id, body.question, db)
                        log.generated_sql = sql
                        db.commit()
                        yield _sse("sql", {"sql": sql, "dialect": dialect, "permission": "待校验"})
                        try:
                            exec_result = run_query(datasource_id, sql, user.id, dialect=dialect)
                        except Exception as exc2:  # noqa: BLE001
                            logger.warning("修正后仍执行失败: %s", exc2)
                            yield _sse("error", {"code": "SQL_EXEC_FAILED", "msg": str(exc2)})
                            log.executed = False
                            log.latency_ms = int((time.time() - start) * 1000)
                            db.commit()
                            yield _sse("done", {})
                            return
                    else:
                        yield _sse("error", {"code": "SQL_EXEC_FAILED", "msg": str(exc)})
                        log.executed = False
                        log.latency_ms = int((time.time() - start) * 1000)
                        db.commit()
                        yield _sse("done", {})
                        return
                else:
                    yield _sse("error", {"code": "SQL_EXEC_FAILED", "msg": str(exc)})
                    log.executed = False
                    log.latency_ms = int((time.time() - start) * 1000)
                    db.commit()
                    yield _sse("done", {})
                    return

            log.executed = True
            log.llm_used = bool(llm and llm.configured)
            log.permission_injected = exec_result["permission"]
            log.row_count = exec_result["row_count"]
            log.latency_ms = int((time.time() - start) * 1000)
            db.commit()

            yield _sse("execute", {"status": "ok", "row_count": exec_result["row_count"],
                                   "latency_ms": exec_result["latency_ms"],
                                   "permission": exec_result["permission"]})
            rows = to_jsonable(exec_result["rows"])
            columns = exec_result["columns"]
            # 列头中文化兜底：英文列名替换为元数据中文注释（模板/LLM 双路径）
            try:
                from ..engine.nl2sql import localize_result_columns
                columns = localize_result_columns(sql, columns, datasource_id)
            except Exception as _e:  # noqa: BLE001
                logger.warning("列头中文化失败: %s", _e)
            yield _sse("table", {"columns": columns, "rows": rows,
                                 "total": exec_result["row_count"],
                                 "truncated": exec_result["truncated"]})

            # ===== L4 图表（意图驱动）=====
            yield _sse("progress", {"stage": "chart", "msg": "正在生成图表…"})
            chart = build_chart_option(columns, rows, intent=spec.intent)
            log.chart_type = chart.get("type", "")
            db.commit()
            yield _sse("chart", chart)

            # ===== L4 确定性事实 + 异常检测 =====
            compare = None
            if spec.intent == "compare":
                compare = compute_compare_facts(columns, rows)
            facts = compute_facts(spec, columns, rows, compare=compare)
            anomalies = detect_anomalies(spec, columns, rows, compare=compare)
            # 空数据补充标注（SUM 空集返回 NULL 等场景）
            if facts.get("empty") and not any(a["type"] == "empty" for a in anomalies):
                anomalies.insert(0, {"type": "empty",
                                     "desc": "该周期无对应数据（指标为空），可尝试扩大时间范围或调整筛选条件"})
            log.anomaly_json = anomalies
            db.commit()

            # ===== L4 四层结论（确定性 + LLM 解读）=====
            yield _sse("progress", {"stage": "summary", "msg": "正在整理最终结果…"})
            sections = _build_summary_sections(spec, facts, anomalies, chart, llm)
            log.summary_sections_json = sections
            db.commit()
            summary_text = _sections_to_text(sections, anomalies)
            yield _sse("summary", {"text": summary_text, "sections": sections,
                                    "anomalies": anomalies})

            # ===== L5 溯源 =====
            trace = {
                "intent": spec.intent,
                "spec": spec_to_dict(spec),
                "schema": spec.schema or "",
                "mapping": {k: v for k, v in (mapping or {}).items() if k != "table"},
                "sql": exec_result["sql"],
                "permission": exec_result["permission"],
                "tables": [m["table"] for m in (mapping or {}).get("metrics", [])],
                "latency_ms": exec_result["latency_ms"],
                "time": spec.time.model_dump(mode="json") if spec.time else None,
                "template_sql": used_template,
                "selected_tables": llm_selected_tables or [],
                "select_source": llm_select_source or ("template" if used_template else ""),
            }
            yield _sse("trace", trace)

            # 结果落库（含 QuerySpec / trace / anomalies / sections）
            _content_json = {
                "text": summary_text,
                "sections": sections,
                "anomalies": anomalies,
                "sql": exec_result["sql"],
                "permission": exec_result["permission"],
                "columns": columns,
                "rows": rows,
                "row_count": exec_result["row_count"],
                "chart": chart,
                "query_spec": spec_to_dict(spec),
                "trace": trace,
            }
            _content_json = json.loads(json.dumps(_content_json, default=str))
            db.add(ConversationMessage(
                conversation_id=conv_id, role="assistant", content_type="result",
                content_json=_content_json))
            db.commit()
            yield _sse("done", {})
        except LLMError as exc:
            yield _sse("error", {"code": "LLM_ERROR", "msg": str(exc)})
            yield _sse("done", {})
        except Exception as exc:  # noqa: BLE001
            logger.exception("问数链路异常")
            yield _sse("error", {"code": "INTERNAL", "msg": str(exc)})
            yield _sse("done", {})

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


def _strip_thinking_preamble(text: str) -> str:
    """剥离模型输出的思考过程（编号列表式推理段），只保留最终回答。"""
    t = (text or "").strip()
    if not t:
        return t
    lines = t.splitlines()
    # 推理段特征：以"数字."开头 或 整体缩进（4 空格/制表符）；最终回答为顶格非编号行
    saw_reasoning = False
    for i, ln in enumerate(lines):
        if not ln.strip():
            continue
        if re.match(r"^\s*\d+[\.、)]", ln):
            saw_reasoning = True
            continue
        if ln.startswith(("    ", "\t")):
            continue
        # 顶格非编号行：若之前出现过推理段，视为最终回答起点
        if saw_reasoning:
            out = "\n".join(lines[i:]).strip()
            return out or t
    # 全部是推理段，没有最终回答
    if saw_reasoning:
        return ""
    return t


def _match_faq_answer(db, ws_id: int, question: str):
    """FAQ 优先匹配：命中且有答案的 FAQ 对时返回 (faq, hit)，否则 (None, None)。
    放在意图识别之前调用：FAQ 能回答的问题直接走 FAQ，不再进入问数链路。"""
    try:
        from ..engine.rag import hybrid_search
        hits = hybrid_search(ws_id, question, kind="faq", top_k=5, min_score=0.65)
        for h in hits:
            faq_id = (h.get("meta") or {}).get("faq_id")
            if not faq_id:
                continue
            faq = db.query(FaqPair).get(faq_id)
            if faq and faq.enabled and (faq.answer or "").strip():
                return faq, h
    except Exception as exc:  # noqa: BLE001
        logger.warning("FAQ 优先匹配失败，继续常规链路: %s", exc)
    return None, None


def _stream_chat_answer(question, history, llm, conv_id, db, log, start):
    """闲聊/通用回答：直接 LLM 流式输出，不检索数据库和知识库。"""
    yield _sse("progress", {"stage": "answer", "msg": "正在生成回答…"})
    messages = [{"role": "system", "content": "你是企业数据问数助手，友好、专业、简洁。直接回答用户问题，可使用 Markdown 格式组织内容。"}]
    for m in (history or [])[-6:]:
        text = m.get("text", "")
        if text:
            messages.append({"role": m.get("role", "user"), "content": text})
    messages.append({"role": "user", "content": question})

    full = ""
    if llm and llm.configured:
        try:
            for chunk in llm.chat_stream(messages, max_tokens=4096):
                r_delta = chunk.get("reasoning", "")
                c_delta = chunk.get("content", "")
                if r_delta:
                    yield _sse("thinking", {"delta": r_delta})
                if c_delta:
                    full += c_delta
                    yield _sse("stream", {"delta": c_delta})
        except Exception as exc:  # noqa: BLE001
            logger.warning("闲聊回答失败: %s", exc)
    # 兜底剥离：正常流式已分离 reasoning/content，此处仅防御异常场景
    full = _strip_thinking_preamble(full)
    if not full.strip():
        full = "你好！我是 AI 问数助手，可以帮你查询分析数据库数据，也可以配置知识库让回答更准确。"
    yield _sse("summary", {"text": full})
    db.add(ConversationMessage(
        conversation_id=conv_id, role="assistant", content_type="result",
        content_json={"text": full}))
    log.intent = "chat"
    log.executed = False
    log.latency_ms = int((time.time() - start) * 1000)
    db.commit()
    yield _sse("done", {})


def _stream_knowledge_answer(question, history, llm, conv_id, db, log, start, ws_id):
    """知识问答：FAQ 已在意图识别前优先匹配，此处仅检索文档；未命中文档降级闲聊。"""
    yield _sse("progress", {"stage": "retrieve", "msg": "正在检索知识库文档…"})
    from ..engine.rag import hybrid_search
    doc_hits = hybrid_search(ws_id, question, kind="doc", top_k=5, min_score=0.5)

    if not doc_hits:
        yield _sse("progress", {"stage": "fallback", "msg": "未检索到知识库内容，降级为通用回答…"})
        yield from _stream_chat_answer(question, history, llm, conv_id, db, log, start)
        return

    yield from _finish_knowledge(question, {"faq": [], "doc": doc_hits}, llm, conv_id, db, log, start)


def _finish_knowledge(question, rag_hits, llm, conv_id, db, log, start):
    """知识问答收尾：FAQ 命中直接返回答案，否则文档 RAG + LLM 生成。"""
    faq_hits = rag_hits.get("faq") or []
    doc_hits = rag_hits.get("doc") or []
    yield _sse("progress", {"stage": "retrieve",
                             "msg": f"知识库命中：FAQ {len(faq_hits)} 条 / 文档 {len(doc_hits)} 条"})

    if faq_hits:
        _db2 = SessionLocal()
        try:
            f = _db2.query(FaqPair).get(faq_hits[0]["meta"].get("faq_id"))
            answer = (f.answer if f and (f.answer or "").strip()
                      else f"Q：{f.question}\nA：（该 FAQ 未填写答案）" if f else "")
        finally:
            _db2.close()
        if answer:
            yield _sse("summary", {"text": answer})
            db.add(ConversationMessage(
                conversation_id=conv_id, role="assistant", content_type="result",
                content_json={"text": answer}))
            log.intent = "knowledge"
            log.executed = False
            log.latency_ms = int((time.time() - start) * 1000)
            db.commit()
            yield _sse("done", {})
            return

    yield _sse("progress", {"stage": "summary", "msg": "正在生成答案…"})
    ctx_parts = []
    for i, h in enumerate(doc_hits, 1):
        ctx_parts.append(f"[文档{i}] {h['content'][:800]}")
    for i, h in enumerate(faq_hits, 1):
        ctx_parts.append(f"[FAQ{i}] {h['content'][:300]}")
    if ctx_parts and llm and llm.configured:
        answer = llm.chat([
            {"role": "system",
             "content": "你是企业知识助手。请仅依据【参考资料】回答用户问题，组织成简洁、有条理、可直接阅读的最终答案；资料不足时如实说明。直接输出答案正文，不要输出思考过程。"},
            {"role": "user",
             "content": f"【参考资料】\n" + "\n".join(ctx_parts) + f"\n\n【用户问题】{question}"},
        ], max_tokens=2048, thinking=False)
        if not (answer or "").strip() or "Thinking Process" in answer[:80]:
            answer = "知识库检索结果：\n" + "\n".join(
                f"· {h['content'][:260]}" for h in doc_hits[:4])
    elif ctx_parts:
        answer = "知识库命中：\n" + "\n".join(
            f"· {h['content'][:120]}" for h in (doc_hits + faq_hits)[:3])
    else:
        answer = "知识库中暂未找到与问题相关的内容。"
    yield _sse("summary", {"text": answer})
    db.add(ConversationMessage(
        conversation_id=conv_id, role="assistant", content_type="result",
        content_json={"text": answer}))
    log.intent = "knowledge"
    log.executed = False
    log.latency_ms = int((time.time() - start) * 1000)
    db.commit()
    yield _sse("done", {})


# ---------- 四层结论（确定性计算 + LLM 解读） ----------
def _fmt_num(v, unit: str = "") -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        if abs(v) >= 10000:
            return f"{v / 10000:.2f}万{unit}"
        return f"{v:,.2f}{unit}"
    return f"{v:,}{unit}"


def _time_label(spec) -> str:
    t = spec.time
    if t and t.expr:
        return t.expr
    if t and t.start and t.end:
        return f"{t.start}~{t.end}"
    return "指定周期"


def _disp_metric(name: str | None) -> str:
    """指标名展示清洗：剥 __count__ 内部前缀，空值兜底「指标」。"""
    return (name[len("__count__:"):] if name and name.startswith("__count__:") else name) or "指标"


def _build_summary_sections(spec, facts: dict, anomalies: list[dict],
                            chart: dict, llm: LLMClient | None) -> dict:
    """四层结论：核心数据结果 / 变化分析 / 亮点与异常 / 极简总结。

    数字全部来自 facts（确定性计算），LLM 只做业务化解读（硬约束：不得新增数字）。
    """
    intent = spec.intent
    tl = _time_label(spec)
    m_label = facts.get("metric_label") or _disp_metric(spec.metrics[0].name if spec.metrics else "")

    # ---- 第 1 层：核心数据结果（确定性） ----
    if intent == "value":
        result = f"{tl}「{m_label}」为 {_fmt_num(facts.get('current'))}。"
    elif intent == "compare":
        cur, prev, diff, rate = (facts.get("current"), facts.get("previous"),
                                 facts.get("diff"), facts.get("rate"))
        if prev is not None and rate is not None:
            result = (f"{tl}「{m_label}」为 {_fmt_num(cur)}，"
                      f"较上期 {_fmt_num(prev)}（{diff:+,.2f}，{rate:+.2f}%）。")
        else:
            result = f"{tl}「{m_label}」为 {_fmt_num(cur)}（上期无数据）。"
    elif intent == "ranking":
        if facts.get("empty"):
            result = f"{tl}「{m_label}」榜单：该周期无对应数据。"
        else:
            result = (f"{tl}「{m_label}」榜单共 {facts.get('row_count', 0)} 项，"
                      f"第 1 名 {facts.get('top_name', '—')}（{_fmt_num(facts.get('top_value'))}）。")
    elif intent == "trend":
        if facts.get("empty"):
            result = f"{tl}「{m_label}」趋势：共 {facts.get('row_count', 0)} 个数据点，指标数据为空。"
        else:
            result = (f"{tl}「{m_label}」趋势：共 {facts.get('row_count', 0)} 个数据点，"
                      f"自 {_fmt_num(facts.get('first'))} 至 {_fmt_num(facts.get('last'))}"
                      + (f"（累计变化 {facts.get('change', 0):+.2f}%）。" if facts.get("change") is not None else "。"))
    elif intent == "statistic":
        if facts.get("multi_metric"):
            # 多指标分类对比（如：每个项目的接口用例数、功能用例数分别是多少）
            dim_col = facts.get("dim_col", "维度")
            metric_cols = facts.get("metric_cols", [])
            detail = facts.get("detail_rows", [])
            parts = [f"共 {facts.get('row_count', 0)} 个{dim_col}，各{dim_col}的{ '、'.join(metric_cols) }分别为："]
            for row in detail[:10]:
                name = row.get(dim_col, "—")
                vals = "，".join(f"{mc}：{_fmt_num(row.get(mc))}" for mc in metric_cols)
                parts.append(f"  · {name}：{vals}")
            if len(detail) > 10:
                parts.append(f"  · …（共 {len(detail)} 项，详见表格）")
            # 各指标合计
            totals = []
            for mc in metric_cols:
                t = facts.get(f"total_{mc}")
                if t is not None:
                    totals.append(f"{mc}合计 {_fmt_num(t)}")
            if totals:
                parts.append("汇总：" + "，".join(totals) + "。")
            result = "\n".join(parts)
        elif spec.action.stat == "ratio":
            if facts.get("top_ratio") is not None:
                result = (f"{tl}「{m_label}」构成分析：共 {facts.get('row_count', 0)} 个分类，"
                          f"合计 {_fmt_num(facts.get('total'))}，占比最高"
                          f" {facts.get('top_name', '—')}（{_fmt_num(facts.get('top_ratio'))}%）。")
            else:
                result = (f"{tl}「{m_label}」构成分析：共 {facts.get('row_count', 0)} 个分类，"
                          f"合计 {_fmt_num(facts.get('total'))}；指标数据为空，暂无法计算各分类占比。")
        else:
            stat_label = {"avg": "平均值", "max": "最大值", "min": "最小值",
                          "sum": "合计", "distinct": "去重数"}.get(spec.action.stat or "sum", "数值")
            result = f"{tl}「{m_label}」{stat_label}为 {_fmt_num(facts.get('current'))}。"
    elif intent == "detail":
        result = (f"{tl}共返回 {facts.get('row_count', 0)} 条明细"
                  + (f"，字段：{'、'.join(facts.get('columns') or [])}" if facts.get("columns") else "")
                  + "。")
    else:
        result = f"{tl}查询完成，共 {facts.get('row_count', 0)} 行。"
    if not result.endswith(("。", "！", "？", "；")):
        result += "。"

    # ---- 第 2/4 层：变化分析 + 一句话总结（LLM 解读，失败回退模板） ----
    has_data = any(facts.get(k) is not None for k in ("current", "last", "top_value", "total"))
    change, summary = "", ""
    if has_data:
        change, summary = _llm_interpret(spec, facts, anomalies, llm)
    if not change:
        if intent == "compare" and facts.get("rate") is not None:
            change = f"较上期 {facts['rate']:+.2f}%，"
            if abs(facts["rate"]) >= 20:
                change += "变化幅度较大，建议关注业务动态。"
            else:
                change += "整体平稳。"
        elif intent == "trend" and facts.get("change") is not None:
            change = (f"周期内累计变化 {facts['change']:+.2f}%，"
                      + ("整体上行。" if facts["change"] > 0 else "整体下行。"))
        elif intent == "statistic" and facts.get("top_name") and facts.get("top_ratio") is not None:
            change = f"占比最高的分类是 {facts['top_name']}。"
        else:
            change = ""
    if not summary:
        if facts.get("empty"):
            summary = "该周期无对应数据，建议扩大时间范围或调整筛选条件后重查。"
        elif facts.get("multi_metric"):
            metric_cols = facts.get("metric_cols", [])
            dim_col = facts.get("dim_col", "维度")
            summary = f"各{dim_col}的{'、'.join(metric_cols)}已分别统计，可结合分组柱状图对比查看。"
        elif intent == "compare" and facts.get("rate") is not None:
            summary = f"「{m_label}」整体表现"
            summary += "优于上期。" if facts["rate"] > 0 else "弱于上期。"
        elif intent == "ranking" and facts.get("top_name"):
            summary = f"「{facts.get('top_name')}」在「{m_label}」上领先。"
        elif intent == "trend" and facts.get("change") is not None:
            summary = f"「{m_label}」呈{'上升' if facts['change'] > 0 else '下降'}趋势。"
        else:
            summary = f"「{m_label}」数据已返回，可结合图表查看。"
    if not summary.endswith("。"):
        summary += "。"

    # ---- 第 3 层：亮点与异常 ----
    highlight = ""
    if anomalies:
        highlight = "；".join(a["desc"] for a in anomalies[:2]) + "。"
    elif facts.get("multi_metric"):
        # 多指标对比：列出各指标最高项
        tops = []
        for mc in facts.get("metric_cols", []):
            t = facts.get(f"top_{mc}")
            if t:
                tops.append(f"{mc}最高：{t['name']}（{_fmt_num(t['value'])}）")
        if tops:
            highlight = "；".join(tops) + "。"
    elif not facts.get("empty") and intent == "ranking" and facts.get("top_name"):
        highlight = f"榜首：{facts['top_name']}（{_fmt_num(facts.get('top_value'))}）。"
    elif intent == "trend" and facts.get("peak"):
        highlight = f"峰值出现在 {facts['peak']}。"
    elif intent == "statistic" and facts.get("top_name") and facts.get("top_ratio") is not None:
        highlight = f"头部集中度：{facts['top_name']} 占 {_fmt_num(facts.get('top_ratio'))}%。"

    return {"result": result, "change": change, "highlight": highlight, "summary": summary}


def _llm_interpret(spec, facts: dict, anomalies: list[dict],
                   llm: LLMClient | None) -> tuple[str, str]:
    """LLM 业务化解读：输入确定性事实，硬约束不得新增数字。失败返回空串（回退模板）。"""
    if llm is None or not llm.configured:
        return "", ""
    try:
        import json as _json
        fact_text = _json.dumps(facts, ensure_ascii=False)
        anomaly_text = _json.dumps(anomalies, ensure_ascii=False)
        raw = llm.chat([
            {"role": "system",
             "content": "你是资深数据分析师。基于【确定性事实】（JSON，所有数字和名称可信，"
                        "不得新增任何数字、不得翻译或改写事实中的分类名、不得推测事实之外的内容），"
                        "输出两句简洁中文：一句「变化分析」（解读各指标对比/排名/差异，若无可写空字符串），"
                        "一句「一句话总结」。多指标对比场景需分别说明各指标的表现和差异。"
                        "只输出 JSON：{\"change\": \"...\", \"summary\": \"...\"}。"},
            {"role": "user",
             "content": f"【确定性事实】{fact_text}\n【异常标注】{anomaly_text}\n"
                        f"【意图】{spec.intent}\n【指标】{_disp_metric(spec.metrics[0].name) if spec.metrics else ''}"},
        ], max_tokens=600, temperature=0.3, thinking=False)
        data = json.loads(raw) if raw.strip().startswith("{") else {}
        change = (data.get("change") or "").strip()
        summary = (data.get("summary") or "").strip()
        return change, summary
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM 结论解读失败: %s", exc)
        return "", ""


def _sections_to_text(sections: dict, anomalies: list[dict]) -> str:
    """四层结论拼为整体文本（兼容旧前端纯文本渲染）。"""
    parts = [sections.get("result", "")]
    if sections.get("change"):
        parts.append(sections["change"])
    if sections.get("highlight"):
        parts.append(sections["highlight"])
    parts.append(sections.get("summary", ""))
    return "\n".join(p for p in parts if p)


# ---------- 会话历史 ----------
@router.get("/conversations")
def list_conversations(db: Session = Depends(get_db), user=Depends(get_current_user)):
    rows = (db.query(Conversation)
            .filter(Conversation.workspace_id == user.workspace_id,
                    Conversation.user_id == user.id)
            .order_by(Conversation.id.desc()).limit(100).all())
    return [{"id": c.id, "title": c.title, "create_time": c.create_time} for c in rows]


@router.get("/conversations/{conv_id}/messages")
def get_messages(conv_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    conv = db.query(Conversation).get(conv_id)
    if not conv or conv.user_id != user.id:
        raise HTTPException(404, "会话不存在")
    rows = (db.query(ConversationMessage)
            .filter(ConversationMessage.conversation_id == conv_id)
            .order_by(ConversationMessage.id).all())
    return [{"id": m.id, "role": m.role, "content_type": m.content_type,
             "content": m.content_json, "create_time": m.create_time} for m in rows]


# ---------- 反馈 ----------
@router.post("/messages/{msg_id}/feedback")
def feedback(msg_id: int, body: FeedbackIn, db: Session = Depends(get_db),
             user=Depends(get_current_user)):
    log = db.query(QueryLog).filter(QueryLog.id == msg_id).first()
    if not log:
        msg = db.query(ConversationMessage).get(msg_id)
        if not msg:
            raise HTTPException(404, "消息不存在")
        log = (db.query(QueryLog)
               .filter(QueryLog.conversation_id == msg.conversation_id,
                       QueryLog.question == (msg.content_json or {}).get("text", ""))
               .order_by(QueryLog.id.desc()).first())
        if not log:
            raise HTTPException(404, "未找到对应日志")
    log.feedback = body.feedback
    log.feedback_note = body.note
    db.commit()
    return {"ok": True}


# ---------- 结果保存复用 ----------
class SaveQueryIn(BaseModel):
    name: str
    params: list[dict] = []
    chart_config: dict = {}
    tags: str = ""
    remark: str = ""


@router.post("/chat/{msg_id}/save-query")
def save_query(msg_id: int, body: SaveQueryIn, db: Session = Depends(get_db),
               user=Depends(get_current_user)):
    msg = db.query(ConversationMessage).get(msg_id)
    if not msg or msg.content_type != "result":
        raise HTTPException(404, "消息不存在")
    content = msg.content_json or {}
    if not content.get("sql"):
        raise HTTPException(400, "该消息无可用 SQL")
    sq = SavedQuery(workspace_id=user.workspace_id, owner_id=user.id, name=body.name,
                    sql_text=content["sql"], params_json=body.params,
                    chart_config_json=body.chart_config, tags=body.tags, remark=body.remark)
    db.add(sq)
    db.commit()
    db.refresh(sq)
    return {"id": sq.id, "name": sq.name}


from ..database import SessionLocal  # noqa: E402
