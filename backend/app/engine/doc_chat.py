"""文件问答主逻辑：文件检索 → Prompt 组装 → LLM 流式生成 → 引用溯源。
不走意图识别/SQL，仅基于上传的文件内容回答（不检索系统知识库）。"""
from __future__ import annotations

import json
import logging
import time

from .doc_retriever import (FINAL_TOP_K, MIN_SCORE, embed_question,
                             format_context, retrieve_file_chunks)
from .doc_store import DocSessionFile, get_store
from ..database import SessionLocal
from .llm_provider import resolve_llm_client, resolve_llm_client_by_id
from ..llm import LLMError

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一个文档问答助手。请严格根据本轮提供的【参考资料】回答用户问题。

【硬性规则】
1. 仅基于本轮【参考资料】回答，不得使用资料外的知识，不得编造数据、日期、人名、条款等信息
2. 如果参考资料中没有相关信息，明确回答"未找到相关内容"，不要猜测或编造
3. 回答中引用内容时，在对应句子末尾标注引用编号，如 [1] [2]，编号只对应本轮【参考资料】的序号，禁止引用历史对话里出现过、但本轮资料中不存在的编号
4. 回答简洁、准确，直接给出结论，不重复原文，不输出思考过程
5. 涉及数字、日期、金额时，必须与原文完全一致，不得擅自换算或四舍五入
6. 不执行任何数据库查询或 SQL，所有信息均来自提供的文档内容
7. 用中文回答，语言自然流畅，适合业务人员阅读

【多轮对话协议（保证上下文一致、结果可继续流转）】
1. 必须结合历史对话理解当前问题：当前问题中的"它/这个/上面那个/继续/再详细点"等代词、省略式追问，都要先还原为历史中实际讨论的对象再作答；
2. 但事实依据只能来自本轮【参考资料】：历史对话只用于消解指代、保持口径一致，不得把历史回答里的数字/结论当作新的事实来源；
3. 若当前问题脱离了参考资料（如让你创作、查数据库、回答常识），直接说明"未找到相关内容"或"我只能基于所选文件回答"，不要发散；
4. 输出为可直接展示给用户的自然语言正文，不要输出 JSON、不要输出 role 标记。"""


def build_user_prompt(question: str, context: str) -> str:
    return f"""【参考资料】
{context}

【用户问题】
{question}

请根据上述参考资料回答用户问题，在引用处标注 [1] [2] 等编号。"""


def doc_chat_stream(
    question: str,
    file_ids: list[str],
    session_id: str,
    workspace_id: int,
    model_id: int | None = None,
    history: list[dict] | None = None,
) -> list[dict]:
    """文件问答流式生成器，yield {"event": ..., "data": ...} 事件。

    Args:
        question: 用户问题
        file_ids: 上传文件 ID 列表
        session_id: 会话 ID（用于从 DocStore 取文件）
        workspace_id: 工作区 ID（用于知识库检索和模型加载）
        model_id: 用户选择的模型 ID
        history: 历史对话（多轮上下文）

    Yields:
        dict: {"event": str, "data": dict} 事件
    """
    start_time = time.time()
    history = history or []

    # 1. 检查文件状态
    store = get_store()
    ready_files = store.get_ready_files(session_id, file_ids)
    all_status = store.get_status(session_id, file_ids)
    not_ready = [s for s in all_status if s["status"] != "ready"]

    if not ready_files and not file_ids:
        yield {"event": "error", "data": {"code": "EMPTY_QUESTION", "msg": "未选择文件"}}
        return

    if not_ready and not ready_files:
        # 所有文件都未就绪
        failed = [s for s in not_ready if s["status"] == "failed"]
        if failed:
            yield {"event": "error", "data": {
                "code": "FILE_PARSE_FAILED",
                "msg": f"文件解析失败: {failed[0].get('fail_reason', '未知错误')}",
            }}
        else:
            yield {"event": "error", "data": {
                "code": "FILE_NOT_READY",
                "msg": "文件解析中，请稍候再试",
            }}
        return

    if ready_files:
        yield {"event": "progress", "data": {"msg": f"已加载 {len(ready_files)} 个文件，正在检索..."}}

    # 2. 问题向量化
    question_vector = embed_question(question, workspace_id)

    # 3. 文件检索（不检索系统知识库，仅基于上传的文档回答）
    hits = retrieve_file_chunks(question_vector, ready_files, top_k=FINAL_TOP_K)
    max_score = max((h["score"] for h in hits), default=0.0)

    # 4. 相关性过滤
    if not hits or max_score < MIN_SCORE:
        logger.info("无相关内容（最高相似度 %.4f < %.2f），不调用 LLM", max_score, MIN_SCORE)
        yield {"event": "answer", "data": {"delta": "未在文件中找到与问题相关的内容。请尝试换一种问法，或确认文件是否包含相关信息。"}}
        yield {"event": "references", "data": {"references": []}}
        yield {"event": "trace", "data": {
            "file_ids": file_ids,
            "retrieved_count": 0,
            "max_score": round(max_score, 4),
            "elapsed_ms": int((time.time() - start_time) * 1000),
        }}
        yield {"event": "done", "data": {}}
        return

    # 5. Prompt 组装
    context = format_context(hits)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    # 历史对话（仅最近 4 轮，避免上下文过长）
    for h in history[-4:]:
        if h.get("text"):
            messages.append({"role": h["role"], "content": h["text"]})
    messages.append({"role": "user", "content": build_user_prompt(question, context)})

    # 6. 加载 LLM 客户端
    db = SessionLocal()
    try:
        if model_id:
            llm = resolve_llm_client_by_id(db, model_id, workspace_id)
        else:
            llm = resolve_llm_client(db, workspace_id, scene="sql")
        if llm is None:
            from ..llm import LLMClient
            llm = LLMClient()
    finally:
        db.close()

    # 7. LLM 流式生成
    full_answer = ""
    try:
        for chunk in llm.chat_stream(messages, temperature=0.3, thinking=False):
            delta = chunk.get("content", "") if isinstance(chunk, dict) else str(chunk)
            if delta:
                full_answer += delta
                yield {"event": "answer", "data": {"delta": delta}}
    except LLMError as exc:
        logger.error("文件问答 LLM 调用失败: %s", exc)
        yield {"event": "error", "data": {"code": "LLM_ERROR", "msg": f"模型调用失败: {exc}"}}
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("文件问答异常")
        yield {"event": "error", "data": {"code": "LLM_ERROR", "msg": str(exc)}}
        return

    # 8. 引用来源（去重，保留命中的前 8 条）
    references = []
    seen_content = set()
    for h in hits[:8]:
        key = h["content"][:100]
        if key in seen_content:
            continue
        seen_content.add(key)
        ref = {
            "source_type": h["source_type"],
            "content": h["content"][:300],
            "score": round(h["score"], 4),
        }
        if h["source_type"] == "file":
            ref["file_name"] = h.get("file_name")
            ref["page_num"] = h.get("page_num")
            ref["sheet_name"] = h.get("sheet_name")
        else:
            ref["doc_id"] = h.get("doc_id")
            ref["title"] = h.get("title")
        references.append(ref)

    yield {"event": "references", "data": {"references": references}}

    # 9. trace
    yield {"event": "trace", "data": {
        "file_ids": file_ids,
        "ready_file_count": len(ready_files),
        "retrieved_count": len(hits),
        "file_hits": sum(1 for h in hits if h["source_type"] == "file"),
        "knowledge_hits": sum(1 for h in hits if h["source_type"] == "knowledge"),
        "max_score": round(max_score, 4),
        "answer_length": len(full_answer),
        "elapsed_ms": int((time.time() - start_time) * 1000),
    }}

    yield {"event": "done", "data": {}}
