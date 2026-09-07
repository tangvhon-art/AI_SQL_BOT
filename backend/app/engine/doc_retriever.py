"""联合检索：上传文件（内存向量）+ 系统知识库（hybrid_search）→ 统一排序。"""
from __future__ import annotations

import logging

import numpy as np

from .doc_store import DocSessionFile
from .rag import hybrid_search
from ..database import SessionLocal
from ..models import KnowledgeDoc

logger = logging.getLogger(__name__)

# 相似度阈值：低于此值视为无相关内容
MIN_SCORE = 0.3
# 最终返回 top_k
FINAL_TOP_K = 8
# 文件检索 top_k
FILE_TOP_K = 5
# 知识库检索 top_k
KB_TOP_K = 5


def cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    if denom < 1e-8:
        return 0.0
    return float(np.dot(a, b) / denom)


def embed_question(question: str, workspace_id: int = 1) -> np.ndarray:
    """问题向量化，复用 embedding 客户端。"""
    from ..llm import LLMClient
    from .llm_provider import resolve_embed_client
    db = SessionLocal()
    try:
        llm = resolve_embed_client(db, workspace_id) or LLMClient()
    finally:
        db.close()
    if not llm.configured:
        return np.zeros(1)
    try:
        vecs = llm.embed([question])
        return np.array(vecs[0], dtype=np.float32)
    except Exception as exc:  # noqa: BLE001
        logger.warning("问题向量化失败: %s", exc)
        return np.zeros(1)


def retrieve_file_chunks(question_vector: np.ndarray, ready_files: list[DocSessionFile],
                         top_k: int = FILE_TOP_K) -> list[dict]:
    """对内存中的文件 chunk 做余弦相似度检索。"""
    if question_vector.size <= 1 or not ready_files:
        return []
    hits: list[dict] = []
    for f in ready_files:
        for chunk in f.chunks:
            if chunk.vector is None or chunk.vector.size <= 1:
                continue
            score = cosine_sim(question_vector, chunk.vector)
            if score > 0:
                hits.append({
                    "source_type": "file",
                    "content": chunk.content,
                    "score": score,
                    "file_name": chunk.file_name or f.file_name,
                    "page_num": chunk.page_num,
                    "sheet_name": chunk.sheet_name,
                    "doc_id": None,
                    "title": None,
                })
    hits.sort(key=lambda x: x["score"], reverse=True)
    return hits[:top_k]


def _kb_doc_name(doc_id: int | None) -> str | None:
    """查知识库文档名。"""
    if not doc_id:
        return None
    db = SessionLocal()
    try:
        doc = db.query(KnowledgeDoc).get(doc_id)
        return doc.name if doc else None
    except Exception:  # noqa: BLE001
        return None
    finally:
        db.close()


def retrieve_knowledge(workspace_id: int, question: str, top_k: int = KB_TOP_K) -> list[dict]:
    """检索系统知识库（复用现有 hybrid_search）。"""
    try:
        raw_hits = hybrid_search(workspace_id, question, kind="doc", top_k=top_k)
    except Exception as exc:  # noqa: BLE001
        logger.warning("知识库检索失败: %s", exc)
        return []
    results: list[dict] = []
    for h in raw_hits:
        meta = h.get("meta") or {}
        doc_id = meta.get("doc_id")
        results.append({
            "source_type": "knowledge",
            "content": h.get("content", ""),
            "score": float(h.get("score", 0)),
            "file_name": None,
            "page_num": None,
            "sheet_name": None,
            "doc_id": doc_id,
            "title": _kb_doc_name(doc_id),
        })
    return results


def joint_retrieve(question: str, question_vector: np.ndarray,
                   ready_files: list[DocSessionFile], workspace_id: int,
                   top_k: int = FINAL_TOP_K) -> tuple[list[dict], float]:
    """联合检索：文件 + 知识库，统一排序。

    Returns:
        (hits, max_score): 检索结果列表（按相似度降序），最高相似度得分
    """
    file_hits = retrieve_file_chunks(question_vector, ready_files)
    kb_hits = retrieve_knowledge(workspace_id, question)

    all_hits = file_hits + kb_hits
    all_hits.sort(key=lambda x: x["score"], reverse=True)
    final = all_hits[:top_k]

    max_score = max((h["score"] for h in final), default=0.0)
    logger.info("联合检索: 文件命中 %d, 知识库命中 %d, 最终 %d, 最高相似度 %.4f",
                len(file_hits), len(kb_hits), len(final), max_score)
    return final, max_score


def format_context(hits: list[dict]) -> str:
    """将检索结果格式化为 Prompt 上下文，标注来源类型和编号。"""
    parts: list[str] = []
    for i, h in enumerate(hits, 1):
        if h["source_type"] == "file":
            loc = f"《{h['file_name']}》"
            if h.get("sheet_name"):
                loc += f" Sheet:{h['sheet_name']}"
            if h.get("page_num"):
                loc += f" 第{h['page_num']}页"
            header = f"【文件{i}｜{loc}】"
        else:
            title = h.get("title") or "知识库"
            header = f"【知识库{i}｜{title}】"
        # 截断过长内容（单条 ≤ 800 字符）
        content = h["content"][:800]
        parts.append(f"{header}\n{content}")
    return "\n\n".join(parts)
