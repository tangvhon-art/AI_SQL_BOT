"""审计日志：query_log 检索。"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import QueryLog
from .deps import get_current_user

router = APIRouter(prefix="/query-logs", tags=["audit"])


@router.get("")
def list_logs(question: str = "", intent: str = "", injected: str = "",
              limit: int = 50, db: Session = Depends(get_db), user=Depends(get_current_user)):
    query = db.query(QueryLog).filter(QueryLog.workspace_id == user.workspace_id)
    if question:
        query = query.filter(QueryLog.question.like(f"%{question}%"))
    if intent:
        query = query.filter(QueryLog.intent == intent)
    if injected:
        query = query.filter(QueryLog.permission_injected == injected)
    rows = query.order_by(QueryLog.id.desc()).limit(min(limit, 200)).all()
    return [{"id": r.id, "user_id": r.user_id, "conversation_id": r.conversation_id,
             "question": r.question, "intent": r.intent, "matched_tables": r.matched_tables,
             "generated_sql": r.generated_sql, "permission_injected": r.permission_injected,
             "executed": r.executed, "row_count": r.row_count, "latency_ms": r.latency_ms,
             "chart_type": r.chart_type, "feedback": r.feedback,
             "create_time": r.create_time} for r in rows]
