"""RAG 引擎：文档切片 → Embedding → 向量检索；FAQ/SQL 示例 few-shot 检索。"""
import hashlib
import logging
import re
from typing import Any

from ..database import SessionLocal
from ..llm import LLMClient, LLMError
from ..models import Datasource, DocChunk, FaqPair, SqlExample, Workspace
from .llm_provider import resolve_embed_client, resolve_llm_client
from ..vector_store import VectorStore
from .text_utils import tokenize
from .biz_lexicon import get as lex_get

logger = logging.getLogger(__name__)

CHUNK_SIZE = 600
CHUNK_OVERLAP = 80


def parse_text(raw: str, file_type: str = "txt") -> str:
    """按类型解析文档文本（txt/md 直接读；pdf/docx 预留解析器入口）。"""
    if file_type in ("pdf", "docx"):
        # 预留：接入 PyMuPDF / python-docx 解析器
        return raw
    return raw


def split_chunks(text: str, chunk_size: int = CHUNK_SIZE,
                 overlap: int = CHUNK_OVERLAP) -> list[str]:
    """标题感知 + 固定窗口切片。"""
    text = re.sub(r"\n{3,}", "\n\n", text or "").strip()
    if not text:
        return []
    # 优先按标题段落切分
    paras = re.split(r"(?=^#{1,3}\s)", text, flags=re.M)
    chunks: list[str] = []
    for para in paras:
        para = para.strip()
        if not para:
            continue
        if len(para) <= chunk_size:
            chunks.append(para)
            continue
        # 长段落按窗口滑切
        i = 0
        while i < len(para):
            chunks.append(para[i:i + chunk_size])
            i += chunk_size - overlap
    return chunks


def _embed_or_skip(store: VectorStore, texts: list[str], llm: LLMClient) -> list[list[float]] | None:
    if llm.configured:
        return llm.embed(texts)
    return None


def _hash_embed(text: str) -> list[float]:
    """降级哈希向量（无 LLM 时保证链路可跑通，仅供演示）。"""
    h = hashlib.sha256(text.encode("utf-8")).digest()
    v = [b / 255.0 - 0.5 for b in h]
    return v


def embed_doc_chunks(doc_id: int) -> int:
    """将某文档全部切片向量化写入向量库；返回嵌入条数。"""
    db = SessionLocal()
    store = VectorStore.get()
    llm = resolve_embed_client(db, _owner_ws(doc_id)) or LLMClient()
    try:
        chunks = (db.query(DocChunk)
                  .filter(DocChunk.doc_id == doc_id).all())
        if not chunks:
            return 0
        texts = [c.content for c in chunks]
        vectors: list[list[float]] | None = None
        if llm.configured:
            try:
                vectors = llm.embed(texts)
            except LLMError:
                vectors = None
        if vectors is None:
            vectors = [_hash_embed(t) for t in texts]
        ids = [f"doc-{doc_id}-{c.id}" for c in chunks]
        metas = [{"doc_id": doc_id, "kind": "doc", "seq": c.seq} for c in chunks]
        store.upsert(f"ai_sql_bot_{_owner_ws(doc_id)}_doc", vectors, ids, metas)
        for c in chunks:
            c.embedding_status = "embedded"
        db.commit()
        return len(chunks)
    finally:
        db.close()


def embed_faq(faq_id: int) -> None:
    db = SessionLocal()
    store = VectorStore.get()
    try:
        faq = db.query(FaqPair).get(faq_id)
        if not faq:
            return
        llm = resolve_embed_client(db, faq.workspace_id) or LLMClient()
        chunk = db.query(DocChunk).filter(DocChunk.faq_id == faq_id).first()
        text = f"Q：{faq.question}\nA：{faq.answer}"
        vectors = llm.embed([text]) if llm.configured else [_hash_embed(text)]
        store.upsert(f"ai_sql_bot_{faq.workspace_id}_faq", vectors,
                     [f"faq-{faq_id}"], [{"faq_id": faq_id, "kind": "faq"}])
        if chunk:
            chunk.content = text
            chunk.embedding_status = "embedded"
        else:
            db.add(DocChunk(doc_id=None, faq_id=faq_id, seq=0, content=text,
                            embedding_status="embedded"))
        db.commit()
    finally:
        db.close()


def _owner_ws(doc_id: int) -> int:
    from ..models import KnowledgeDoc
    db = SessionLocal()
    try:
        doc = db.query(KnowledgeDoc).get(doc_id)
        return doc.workspace_id if doc else 0
    finally:
        db.close()


def _entity_overlap(a: str, b: str) -> bool:
    """a 与 b 是否有实质实体词重叠（排除纯疑问通用词）。
    用于 FAQ 命中校验：向量相似但无任何实体词重叠视为误命中。"""
    generic = set(lex_get("generic_question"))
    return bool((tokenize(a) & tokenize(b)) - generic)


def hybrid_search(workspace_id: int, query: str, kind: str = "doc",
                  top_k: int = 5, datasource_id: int | None = None,
                  min_score: float | None = None) -> list[dict]:
    """向量检索 + 关键词召回 → 合并。
    min_score：向量相似度低于该值的命中丢弃（内容与问题不一致时绕过 RAG 增强）。
    关键词精确匹配（FAQ 问题与 query 一致/互为子串）强制放行（score=1.0）。"""
    store = VectorStore.get()
    _tmp = SessionLocal()
    try:
        llm = resolve_embed_client(_tmp, workspace_id) or LLMClient()
    finally:
        _tmp.close()
    collection = f"ai_sql_bot_{workspace_id}_{kind}"
    vec_q = llm.embed([query])[0] if llm.configured else _hash_embed(query)
    vec_hits = store.search(collection, vec_q, top_k=max(top_k, 8))
    if min_score is not None:
        vec_hits = [h for h in vec_hits if h.get("score", 0) >= min_score]
    # 回填向量命中的 content（store 仅存 id/vector/meta）
    db = SessionLocal()
    try:
        for h in vec_hits:
            if "content" in h:
                continue
            meta = h.get("meta") or {}
            if kind == "doc":
                chunk = (db.query(DocChunk)
                         .filter(DocChunk.doc_id == meta.get("doc_id"),
                                 DocChunk.seq == meta.get("seq")).first())
                if chunk:
                    h["content"] = chunk.content
            elif kind == "faq":
                faq = db.query(FaqPair).get(meta.get("faq_id"))
                if faq:
                    h["content"] = f"Q：{faq.question}\nA：{faq.answer}"
    finally:
        db.close()
    if kind == "faq":
        # 实体词校验：向量命中但无实体词重叠的 FAQ 视为误命中，移出
        db2 = SessionLocal()
        try:
            filtered = []
            for h in vec_hits:
                f = db2.query(FaqPair).get((h.get("meta") or {}).get("faq_id"))
                if f and _entity_overlap(query, f.question):
                    filtered.append(h)
            vec_hits = filtered
        finally:
            db2.close()
    # 关键词补充
    words = tokenize(query)
    kw_hits: list[dict] = []
    db = SessionLocal()
    try:
        if kind == "doc":
            cands = (db.query(DocChunk)
                     .filter(DocChunk.embedding_status == "embedded",
                             DocChunk.doc_id.isnot(None)).all())
            for c in cands:
                score = sum(1 for w in words if w in c.content.lower())
                if score:
                    kw_hits.append({"id": f"doc-{c.doc_id}-{c.id}", "score": score / 10,
                                    "meta": {"doc_id": c.doc_id, "kind": "doc", "seq": c.seq},
                                    "content": c.content})
        elif kind == "faq":
            cands = db.query(FaqPair).filter(FaqPair.workspace_id == workspace_id,
                                             FaqPair.enabled.is_(True)).all()
            for f in cands:
                score = sum(1 for w in words
                            if w in f.question.lower() or w in f.answer.lower())
                # 精确匹配放行：问题一致、互为子串、或命中同义问法
                ql = query.strip().lower()
                fl = f.question.strip().lower()
                syns = [x.strip().lower() for x in (f.synonyms_json or []) if x and x.strip()]
                if ql and (ql == fl or (ql in fl or fl in ql) or any(ql == x or ql in x or x in ql for x in syns)):
                    score = 10
                if score:
                    kw_hits.append({"id": f"faq-{f.id}", "score": score / 10,
                                    "meta": {"faq_id": f.id, "kind": "faq"},
                                    "content": f"Q：{f.question}\nA：{f.answer}"})
    finally:
        db.close()
    merged = {h["id"]: h for h in vec_hits}
    for h in kw_hits:
        if h["id"] in merged:
            merged[h["id"]]["score"] = max(merged[h["id"]]["score"], h["score"])
        else:
            merged[h["id"]] = h
    result = sorted(merged.values(), key=lambda x: x["score"], reverse=True)[:top_k]
    # 最终统一按 min_score 过滤（向量与关键词命中一视同仁，低分无关内容绕过）
    if min_score is not None:
        result = [h for h in result if h.get("score", 0) >= min_score]
    return result


def retrieve_few_shot_vectors(workspace_id: int, query: str, top_k: int = 3) -> list[dict]:
    """few-shot 检索（faq 命中带 SQL 示例优先）。"""
    hits = hybrid_search(workspace_id, query, kind="faq", top_k=top_k)
    db = SessionLocal()
    try:
        for h in hits:
            faq_id = h["meta"].get("faq_id")
            if faq_id:
                faq = db.query(FaqPair).get(faq_id)
                h["sql_example"] = faq.sql_example if faq else ""
                h["question"] = faq.question if faq else ""
    finally:
        db.close()
    return hits


def retrieve_doc_context(workspace_id: int, query: str, top_k: int = 3) -> str:
    """检索知识库文档片段，拼成 prompt 参考上下文；无命中返回空串。"""
    hits = hybrid_search(workspace_id, query, kind="doc", top_k=top_k)
    if not hits:
        return ""
    parts = []
    for i, h in enumerate(hits, 1):
        parts.append(f"[资料{i}] {h['content'][:400]}")
    return "\n".join(parts)
