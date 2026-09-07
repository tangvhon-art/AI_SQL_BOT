"""内存文档存储：文件解析 → 切分 → 向量化，按会话隔离，仅内存存储不入数据库。
支持过期自动清理、异步解析、线程安全。"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field

import numpy as np

from .doc_parser import ParseResult, RawPage, parse_file
from ..database import SessionLocal
from .llm_provider import resolve_embed_client
from ..llm import LLMClient

logger = logging.getLogger(__name__)

# 切分参数
CHUNK_MAX_CHARS = 750  # 约 500 token
CHUNK_OVERLAP = 75  # 约 50 token
# 过期时间（秒）
EXPIRE_SECONDS = 30 * 60  # 30 分钟
# 清理间隔
CLEANUP_INTERVAL = 60  # 1 分钟
# 全局 chunk 上限（防内存溢出）
GLOBAL_MAX_CHUNKS = 50000
# embedding 批量大小
EMBED_BATCH_SIZE = 16

TEMP_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "storage", "temp_docs")


@dataclass
class DocChunk:
    """切分后的文档片段，含向量和元数据。"""
    content: str
    vector: np.ndarray | None = None
    page_num: int | None = None
    sheet_name: str | None = None
    file_name: str = ""


@dataclass
class DocSessionFile:
    """会话中的一个文件状态。"""
    file_id: str
    file_name: str
    file_type: str
    file_size: int
    status: str = "pending"  # pending / parsing / ready / failed
    chunks: list[DocChunk] = field(default_factory=list)
    fail_reason: str | None = None
    created_at: float = field(default_factory=time.time)
    last_access: float = field(default_factory=time.time)
    temp_path: str = ""  # 原始文件临时路径，解析后删除

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)

    def to_status_dict(self) -> dict:
        return {
            "file_id": self.file_id,
            "name": self.file_name,
            "file_type": self.file_type,
            "file_size": self.file_size,
            "status": self.status,
            "chunk_count": self.chunk_count,
            "fail_reason": self.fail_reason,
        }


def _split_text(text: str, max_chars: int = CHUNK_MAX_CHARS,
                overlap: int = CHUNK_OVERLAP) -> list[str]:
    """标题感知 + 固定窗口切分（复用 rag.py 思路，独立实现避免循环依赖）。"""
    import re
    text = re.sub(r"\n{3,}", "\n\n", text or "").strip()
    if not text:
        return []
    # 按标题段落切分
    paras = re.split(r"(?=^#{1,3}\s)", text, flags=re.M)
    chunks: list[str] = []
    for para in paras:
        para = para.strip()
        if not para:
            continue
        if len(para) <= max_chars:
            chunks.append(para)
            continue
        i = 0
        while i < len(para):
            chunks.append(para[i:i + max_chars])
            i += max_chars - overlap
    return chunks


def _pages_to_chunks(pages: list[RawPage], file_name: str) -> list[DocChunk]:
    """将解析后的 pages 切分为 DocChunk，保留页码元数据。"""
    chunks: list[DocChunk] = []
    for page in pages:
        if not page.text.strip():
            continue
        sub_chunks = _split_text(page.text)
        for sc in sub_chunks:
            chunks.append(DocChunk(
                content=sc,
                page_num=page.page_num,
                sheet_name=page.sheet_name,
                file_name=file_name,
            ))
    return chunks


class DocStore:
    """全局内存文档存储单例。"""
    _instance: "DocStore | None" = None
    _lock = threading.Lock()

    def __init__(self):
        # session_id -> { file_id -> DocSessionFile }
        self._sessions: dict[str, dict[str, DocSessionFile]] = {}
        self._global_lock = threading.Lock()
        self._cleanup_thread: threading.Thread | None = None
        self._stop_cleanup = threading.Event()
        self._start_cleanup()
        os.makedirs(TEMP_DIR, exist_ok=True)

    @classmethod
    def get(cls) -> "DocStore":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # ---------- 后台清理线程 ----------
    def _start_cleanup(self):
        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._cleanup_thread.start()

    def _cleanup_loop(self):
        while not self._stop_cleanup.is_set():
            try:
                self._cleanup_expired()
            except Exception as exc:  # noqa: BLE001
                logger.warning("文档清理异常: %s", exc)
            self._stop_cleanup.wait(CLEANUP_INTERVAL)

    def _cleanup_expired(self):
        now = time.time()
        expired: list[tuple[str, str]] = []
        with self._global_lock:
            for sid, files in self._sessions.items():
                for fid, f in files.items():
                    if now - f.last_access > EXPIRE_SECONDS:
                        expired.append((sid, fid))
        for sid, fid in expired:
            self._remove_file_internal(sid, fid)
        if expired:
            logger.info("清理过期文件 %d 个", len(expired))

    def _total_chunks(self) -> int:
        with self._global_lock:
            return sum(f.chunk_count for files in self._sessions.values() for f in files.values())

    # ---------- 公共 API ----------

    def add_file(self, session_id: str, file_name: str, file_bytes: bytes,
                 file_type: str) -> DocSessionFile:
        """添加文件到会话，返回 file 对象（status=pending），异步开始解析。"""
        # 全局 chunk 上限检查（LRU 清理最旧的）
        if self._total_chunks() > GLOBAL_MAX_CHUNKS:
            self._evict_oldest()

        file_id = f"file_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
        temp_path = os.path.join(TEMP_DIR, f"{file_id}_{file_name}")
        os.makedirs(os.path.dirname(temp_path), exist_ok=True)
        with open(temp_path, "wb") as f:
            f.write(file_bytes)

        doc_file = DocSessionFile(
            file_id=file_id,
            file_name=file_name,
            file_type=file_type,
            file_size=len(file_bytes),
            temp_path=temp_path,
        )
        with self._global_lock:
            self._sessions.setdefault(session_id, {})[file_id] = doc_file

        # 异步解析
        threading.Thread(
            target=self._process_file,
            args=(session_id, file_id, temp_path, file_name),
            daemon=True,
        ).start()
        return doc_file

    def _process_file(self, session_id: str, file_id: str, temp_path: str, file_name: str):
        """后台线程：解析 → 切分 → 向量化。"""
        try:
            with self._global_lock:
                f = self._sessions.get(session_id, {}).get(file_id)
                if f is None:
                    return
                f.status = "parsing"
                f.last_access = time.time()

            # 1. 解析
            result: ParseResult = parse_file(temp_path, file_name)
            if not result.success:
                with self._global_lock:
                    f = self._sessions.get(session_id, {}).get(file_id)
                    if f:
                        f.status = "failed"
                        f.fail_reason = result.error or "解析失败"
                self._delete_temp(temp_path)
                return

            # 2. 切分
            chunks = _pages_to_chunks(result.pages, file_name)
            if not chunks:
                with self._global_lock:
                    f = self._sessions.get(session_id, {}).get(file_id)
                    if f:
                        f.status = "failed"
                        f.fail_reason = "文件无有效文本内容"
                self._delete_temp(temp_path)
                return

            # 3. 向量化
            vectors = self._embed_chunks(chunks, session_id)
            for i, chunk in enumerate(chunks):
                if i < len(vectors):
                    chunk.vector = vectors[i]

            with self._global_lock:
                f = self._sessions.get(session_id, {}).get(file_id)
                if f:
                    f.chunks = chunks
                    f.status = "ready"
                    f.last_access = time.time()
            logger.info("文件向量化完成: %s, %d chunks", file_name, len(chunks))
        except Exception as exc:  # noqa: BLE001
            logger.exception("文件处理异常: %s", exc)
            with self._global_lock:
                f = self._sessions.get(session_id, {}).get(file_id)
                if f:
                    f.status = "failed"
                    f.fail_reason = f"处理异常: {exc}"
        finally:
            self._delete_temp(temp_path)

    def _embed_chunks(self, chunks: list[DocChunk], session_id: str) -> list[np.ndarray]:
        """批量向量化，失败重试 2 次。"""
        db = SessionLocal()
        try:
            # session_id 可能是 conv_id（int 转 str），workspace 从 user 上下文拿不到，
            # 这里用默认 workspace（embedding 模型配置通常全局共享）
            llm = resolve_embed_client(db, 1) or LLMClient()
        finally:
            db.close()

        if not llm.configured:
            logger.warning("embedding 客户端未配置，文件问答将无法检索")
            return [np.zeros(1) for _ in chunks]

        texts = [c.content for c in chunks]
        all_vectors: list[list[float]] = []
        for start in range(0, len(texts), EMBED_BATCH_SIZE):
            batch = texts[start:start + EMBED_BATCH_SIZE]
            for attempt in range(3):
                try:
                    vecs = llm.embed(batch)
                    all_vectors.extend(vecs)
                    break
                except Exception as exc:  # noqa: BLE001
                    if attempt == 2:
                        logger.error("embedding 批量失败 (start=%d): %s", start, exc)
                        all_vectors.extend([[0.0] * 1024 for _ in batch])
                    else:
                        time.sleep(0.5 * (attempt + 1))
        return [np.array(v, dtype=np.float32) for v in all_vectors]

    def get_status(self, session_id: str, file_ids: list[str] | None = None) -> list[dict]:
        """获取文件状态列表。"""
        with self._global_lock:
            files = self._sessions.get(session_id, {})
            if file_ids:
                result = [files[fid].to_status_dict() for fid in file_ids if fid in files]
            else:
                result = [f.to_status_dict() for f in files.values()]
        # 刷新 last_access
        for r in result:
            self._touch(session_id, r["file_id"])
        return result

    def get_ready_files(self, session_id: str, file_ids: list[str]) -> list[DocSessionFile]:
        """获取指定 file_ids 中状态为 ready 的文件（深拷贝 chunks 列表引用，线程安全读取）。"""
        ready: list[DocSessionFile] = []
        with self._global_lock:
            files = self._sessions.get(session_id, {})
            for fid in file_ids:
                f = files.get(fid)
                if f and f.status == "ready":
                    f.last_access = time.time()
                    ready.append(f)
        return ready

    def remove_file(self, session_id: str, file_id: str) -> bool:
        """移除文件。"""
        return self._remove_file_internal(session_id, file_id)

    def _remove_file_internal(self, session_id: str, file_id: str) -> bool:
        with self._global_lock:
            files = self._sessions.get(session_id, {})
            f = files.pop(file_id, None)
            if not files:
                self._sessions.pop(session_id, None)
        if f and f.temp_path:
            self._delete_temp(f.temp_path)
        return f is not None

    def clear_session(self, session_id: str):
        """清理整个会话的文件。"""
        with self._global_lock:
            files = self._sessions.pop(session_id, {})
        for f in files.values():
            if f.temp_path:
                self._delete_temp(f.temp_path)

    def _touch(self, session_id: str, file_id: str):
        with self._global_lock:
            f = self._sessions.get(session_id, {}).get(file_id)
            if f:
                f.last_access = time.time()

    def _delete_temp(self, path: str):
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except Exception:  # noqa: BLE001
            pass

    def _evict_oldest(self):
        """LRU 清理最旧的会话文件，释放内存。"""
        with self._global_lock:
            all_files = [(sid, fid, f) for sid, files in self._sessions.items()
                         for fid, f in files.items()]
        all_files.sort(key=lambda x: x[2].last_access)
        # 清理最旧的 20%
        evict_count = max(1, len(all_files) // 5)
        for sid, fid, _ in all_files[:evict_count]:
            self._remove_file_internal(sid, fid)
        logger.info("LRU 清理 %d 个旧文件", evict_count)


# 便捷函数
def get_store() -> DocStore:
    return DocStore.get()
