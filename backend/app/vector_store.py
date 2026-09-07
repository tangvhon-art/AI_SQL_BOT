"""向量存储：全局单例 + MySQL 数据库持久化（doc_chunk.embedding），替代本地 JSON 文件。
collection 命名约定：{prefix}_{workspace_id}_{type}，type ∈ doc/faq。
检索按余弦相似度全表扫描（内置演示实现），预留 Milvus/Chroma 适配器（vector_backend 配置切换）。"""
import json
import math
import threading
from typing import Any

from .config import get_settings

_lock = threading.RLock()
_SINGLETON: "BuiltinVectorStore | None" = None


class VectorStore:
    """向量存储接口（单例）。"""

    def upsert(self, collection: str, vectors: list[list[float]], ids: list[str],
               metadatas: list[dict] | None = None) -> None:
        raise NotImplementedError

    def search(self, collection: str, vector: list[float], top_k: int = 5,
               filters: dict | None = None) -> list[dict]:
        raise NotImplementedError

    def delete_by_ids(self, collection: str, ids: list[str]) -> None:
        raise NotImplementedError

    @staticmethod
    def get() -> "VectorStore":
        global _SINGLETON
        backend = get_settings().vector_backend
        with _lock:
            if _SINGLETON is None:
                _SINGLETON = BuiltinVectorStore()  # Milvus/Chroma 适配器按配置接入
            return _SINGLETON


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)


def _parse_collection(collection: str) -> tuple[int, str]:
    """解析 collection → (workspace_id, kind)。格式 ai_sql_bot_{ws}_{doc|faq}"""
    parts = collection.split("_")
    try:
        return int(parts[-2]), parts[-1]
    except (ValueError, IndexError):
        return 0, parts[-1] if parts else "doc"


def _parse_id(item_id: str) -> dict:
    """解析 id → 定位键：doc-{doc_id}-{chunk_id} / faq-{faq_id}"""
    if item_id.startswith("faq-"):
        return {"faq_id": int(item_id.split("-")[1])}
    parts = item_id.split("-")
    # doc-{doc_id}-{chunk_id}
    if len(parts) == 3 and parts[0] == "doc":
        return {"doc_id": int(parts[1]), "chunk_id": int(parts[2])}
    return {}


class BuiltinVectorStore(VectorStore):
    """默认实现：向量持久化到 MySQL（doc_chunk.embedding），服务重启不丢失。"""

    # ---------- 接口 ----------
    def upsert(self, collection, vectors, ids, metadatas=None) -> None:
        from .database import SessionLocal
        from .models import DocChunk

        ws_id, kind = _parse_collection(collection)
        db = SessionLocal()
        try:
            metas = metadatas or [{}] * len(ids)
            for vid, v, m in zip(ids, vectors, metas):
                keys = _parse_id(vid)
                if not keys:
                    continue
                if "faq_id" in keys:
                    chunk = (db.query(DocChunk)
                             .filter(DocChunk.faq_id == keys["faq_id"]).first())
                else:
                    chunk = (db.query(DocChunk)
                             .filter(DocChunk.doc_id == keys.get("doc_id"),
                                     DocChunk.id == keys.get("chunk_id")).first())
                if chunk:
                    chunk.embedding = json.dumps(v)
                    chunk.embedding_status = "embedded"
            db.commit()
        finally:
            db.close()

    def search(self, collection, vector, top_k=5, filters=None) -> list[dict]:
        from .database import SessionLocal
        from .models import DocChunk

        ws_id, kind = _parse_collection(collection)
        db = SessionLocal()
        try:
            q = db.query(DocChunk).filter(DocChunk.embedding.isnot(None))
            if kind == "doc":
                q = q.filter(DocChunk.doc_id.isnot(None))
            else:
                q = q.filter(DocChunk.faq_id.isnot(None))
            rows = q.all()
            scored = []
            for c in rows:
                try:
                    vec = json.loads(c.embedding or "[]")
                except (ValueError, TypeError):
                    continue
                if not vec:
                    continue
                meta = {"kind": kind}
                if c.doc_id is not None:
                    meta.update({"doc_id": c.doc_id, "seq": c.seq})
                if c.faq_id is not None:
                    meta.update({"faq_id": c.faq_id})
                if filters:
                    if any(meta.get(k) != v for k, v in filters.items()):
                        continue
                item_id = f"doc-{c.doc_id}-{c.id}" if c.doc_id is not None else f"faq-{c.faq_id}"
                scored.append((_cosine(vector, vec), item_id, meta))
            scored.sort(key=lambda x: x[0], reverse=True)
            return [{"id": item_id, "score": round(s, 4), "meta": meta}
                    for s, item_id, meta in scored[:top_k]]
        finally:
            db.close()

    def delete_by_ids(self, collection, ids) -> None:
        from .database import SessionLocal
        from .models import DocChunk

        db = SessionLocal()
        try:
            for vid in ids:
                keys = _parse_id(vid)
                if "faq_id" in keys:
                    chunk = (db.query(DocChunk)
                             .filter(DocChunk.faq_id == keys["faq_id"]).first())
                else:
                    chunk = (db.query(DocChunk)
                             .filter(DocChunk.doc_id == keys.get("doc_id"),
                                     DocChunk.id == keys.get("chunk_id")).first())
                if chunk:
                    chunk.embedding = None
                    chunk.embedding_status = "pending"
            db.commit()
        finally:
            db.close()
