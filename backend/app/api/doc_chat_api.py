"""文件问答：文件上传 / 状态查询 / 删除 API。
文件仅内存存储（DocStore），不入数据库，会话级隔离。"""
import logging
import os

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from ..engine.doc_parser import SUPPORTED_EXTS, get_ext
from ..engine.doc_store import get_store
from .deps import get_current_user

router = APIRouter(prefix="/doc-chat", tags=["doc-chat"])
logger = logging.getLogger(__name__)

MAX_FILE_SIZE = 20 * 1024 * 1024  # 20MB
MAX_FILES_PER_SESSION = 5


@router.post("/upload")
async def upload_files(
    files: list[UploadFile] = File(...),
    session_id: str = Form(...),
    user=Depends(get_current_user),
):
    """上传文件（支持多文件），异步解析，返回文件 ID 列表和初始状态。"""
    if not session_id:
        raise HTTPException(400, "缺少 session_id")
    if not files:
        raise HTTPException(400, "未选择文件")

    store = get_store()
    # 检查当前会话文件数
    existing = store.get_status(session_id)
    if len(existing) + len(files) > MAX_FILES_PER_SESSION:
        raise HTTPException(400, f"单会话最多 {MAX_FILES_PER_SESSION} 个文件，当前已有 {len(existing)} 个")

    results = []
    for uf in files:
        file_name = uf.filename or "unnamed"
        ext = get_ext(file_name)
        if ext not in SUPPORTED_EXTS:
            results.append({
                "file_id": None,
                "name": file_name,
                "status": "failed",
                "fail_reason": f"不支持的文件格式: .{ext}",
            })
            continue

        # 读取文件内容（限制大小）
        file_bytes = await uf.read()
        if len(file_bytes) > MAX_FILE_SIZE:
            results.append({
                "file_id": None,
                "name": file_name,
                "status": "failed",
                "fail_reason": f"文件过大（{len(file_bytes) // 1024 // 1024}MB），上限 20MB",
            })
            continue
        if not file_bytes:
            results.append({
                "file_id": None,
                "name": file_name,
                "status": "failed",
                "fail_reason": "文件内容为空",
            })
            continue

        doc_file = store.add_file(session_id, file_name, file_bytes, ext)
        results.append(doc_file.to_status_dict())

    return {"files": results}


@router.get("/status")
def get_file_status(
    session_id: str,
    file_ids: str = "",
    user=Depends(get_current_user),
):
    """批量查询文件解析状态。file_ids 为逗号分隔的 ID 列表，为空则返回会话所有文件。"""
    if not session_id:
        raise HTTPException(400, "缺少 session_id")
    store = get_store()
    fid_list = [fid.strip() for fid in file_ids.split(",") if fid.strip()] if file_ids else None
    files = store.get_status(session_id, fid_list)
    return {"files": files}


@router.delete("/{file_id}")
def delete_file(
    file_id: str,
    session_id: str = "",
    user=Depends(get_current_user),
):
    """从内存中移除指定文件。"""
    if not session_id:
        raise HTTPException(400, "缺少 session_id")
    store = get_store()
    ok = store.remove_file(session_id, file_id)
    if not ok:
        raise HTTPException(404, "文件不存在或已过期")
    return {"ok": True}
