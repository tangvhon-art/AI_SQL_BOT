"""知识库：FAQ / 文档（上传→切片→向量化）/ 检索测试台。"""
import os

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db, soft_delete_all
from ..engine.rag import embed_doc_chunks, embed_faq, hybrid_search, split_chunks
from ..models import DocChunk, FaqPair, KnowledgeDoc, SqlExample
from .common import get_or_404, paginate, workspace_scope
from .deps import get_current_user

router = APIRouter(prefix="", tags=["knowledge"])


class FaqIn(BaseModel):
    question: str
    answer: str = ""
    category: str = ""
    tags: str = ""
    datasource_id: int | None = None
    table_ids: str = ""
    sql_example: str = ""
    synonyms_json: list[str] = []
    enabled: bool = True


def _faq_out(f: FaqPair) -> dict:
    return {"id": f.id, "question": f.question, "answer": f.answer,
            "category": f.category, "tags": f.tags, "datasource_id": f.datasource_id,
            "table_ids": f.table_ids, "sql_example": f.sql_example,
            "synonyms_json": f.synonyms_json or [], "enabled": f.enabled}


# ---------- FAQ ----------
@router.get("/faqs")
def list_faqs(q: str = "", category: str = "", enabled: str = "",
              page: int = 1, size: int = 20, db: Session = Depends(get_db),
              user=Depends(get_current_user)):
    query = workspace_scope(db, FaqPair, user)
    if q:
        query = query.filter(FaqPair.question.like(f"%{q}%"))
    if category:
        query = query.filter(FaqPair.category.like(f"%{category}%"))
    if enabled in ("1", "0"):
        query = query.filter(FaqPair.enabled.is_(enabled == "1"))
    query = query.order_by(FaqPair.id.desc())
    rows, total = paginate(query, page, size)
    return {"total": total, "items": [_faq_out(f) for f in rows]}


@router.post("/faqs")
def create_faq(body: FaqIn, db: Session = Depends(get_db), user=Depends(get_current_user)):
    faq = FaqPair(workspace_id=user.workspace_id, **body.model_dump())
    db.add(faq)
    db.commit()
    db.refresh(faq)
    # 同步生成 SQL 示例（供 few-shot）
    if body.sql_example:
        ex = SqlExample(workspace_id=user.workspace_id, question=body.question,
                        sql_text=body.sql_example, dialect="mysql",
                        table_ids=body.table_ids, source="faq", enabled=True)
        db.add(ex)
        db.commit()
    embed_faq(faq.id)
    return _faq_out(faq)


@router.put("/faqs/{faq_id}")
def update_faq(faq_id: int, body: FaqIn, db: Session = Depends(get_db),
               user=Depends(get_current_user)):
    faq = get_or_404(db, FaqPair, faq_id, "FAQ 不存在")
    for k, v in body.model_dump().items():
        setattr(faq, k, v)
    db.commit()
    embed_faq(faq.id)
    return _faq_out(faq)


@router.delete("/faqs/{faq_id}")
def delete_faq(faq_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    faq = db.query(FaqPair).get(faq_id)
    if faq:
        soft_delete_all(db.query(DocChunk).filter(DocChunk.faq_id == faq_id))
        faq.is_deleted = True
        db.commit()
    return {"ok": True}


# ---------- 文档 ----------
@router.get("/documents")
def list_documents(q: str = "", file_type: str = "", status: str = "",
                   page: int = 1, size: int = 20,
                   db: Session = Depends(get_db), user=Depends(get_current_user)):
    query = workspace_scope(db, KnowledgeDoc, user)
    if q:
        query = query.filter(KnowledgeDoc.name.like(f"%{q}%"))
    if file_type:
        query = query.filter(KnowledgeDoc.file_type == file_type)
    if status:
        query = query.filter(KnowledgeDoc.status == status)
    query = query.order_by(KnowledgeDoc.id.desc())
    rows, total = paginate(query, page, size)
    return {"total": total, "items": [{"id": d.id, "name": d.name, "file_type": d.file_type,
                                       "size": d.size, "status": d.status, "error_msg": d.error_msg,
                                       "version": d.version, "chunk_size": d.chunk_size,
                                       "overlap": d.overlap} for d in rows]}


@router.post("/documents")
async def upload_document(file: UploadFile = File(...), db: Session = Depends(get_db),
                          user=Depends(get_current_user)):
    os.makedirs("uploads", exist_ok=True)
    raw = (await file.read()).decode("utf-8", errors="replace")
    name = file.filename or "unnamed"
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else "txt"
    if ext not in ("txt", "md", "markdown", "pdf", "docx"):
        raise HTTPException(400, "仅支持 txt/md/pdf/docx")
    path = f"uploads/{user.workspace_id}_{name}"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(raw)
    doc = KnowledgeDoc(workspace_id=user.workspace_id, name=name, file_type=ext,
                       file_path=path, size=len(raw.encode("utf-8")),
                       chunk_size=600, overlap=80, status="parsing")
    db.add(doc)
    db.commit()
    db.refresh(doc)
    chunks = split_chunks(raw, doc.chunk_size, doc.overlap)
    for i, c in enumerate(chunks):
        db.add(DocChunk(doc_id=doc.id, seq=i, content=c,
                        token_count=len(c), embedding_status="pending"))
    db.commit()
    # 向量化（后台 worker 场景：异步任务；此处同步演示）
    try:
        n = embed_doc_chunks(doc.id)
        doc.status = "embedded"
        db.commit()
        return {"ok": True, "id": doc.id, "chunks": n}
    except Exception as exc:  # noqa: BLE001
        doc.status = "failed"
        doc.error_msg = str(exc)[:500]
        db.commit()
        raise HTTPException(400, f"向量化失败: {exc}") from exc


@router.delete("/documents/{doc_id}")
def delete_document(doc_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    doc = db.query(KnowledgeDoc).get(doc_id)
    if doc:
        soft_delete_all(db.query(DocChunk).filter(DocChunk.doc_id == doc_id))
        doc.is_deleted = True
        db.commit()
    return {"ok": True}


# ---------- 检索测试台 ----------
class RetrieveIn(BaseModel):
    query: str
    kind: str = "doc"  # doc / faq
    top_k: int = 5


@router.post("/knowledge/retrieve-test")
def retrieve_test(body: RetrieveIn, db: Session = Depends(get_db),
                  user=Depends(get_current_user)):
    hits = hybrid_search(user.workspace_id, body.query, kind=body.kind, top_k=body.top_k)
    return {"query": body.query, "hits": hits}
