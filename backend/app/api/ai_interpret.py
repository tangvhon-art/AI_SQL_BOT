"""AI 解读 API：按需对问数结果 / 报告数据生成 AI 解读（SSE 流式）。

入口：POST /api/v1/ai/interpret
- 问数结果卡片：传 data = {columns, rows} / {chart} / {dashboard} 之一
- 报告中心：传 data = 报告 dashboard_data 快照，并带 report_id（完成后回写报告）

统一使用「Prompt 管理」中 scene_type=ai_interpret 的默认提示词，
可指定 prompt_template_id 覆盖；模型可指定 model_id，否则取工作区默认 SQL 模型。
"""
from __future__ import annotations

import logging
from typing import Generator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import SessionLocal, get_db
from ..engine.ai_interpreter import AiInterpreter
from ..engine.llm_provider import resolve_llm_client, resolve_llm_client_by_id
from ..engine.prompt_service import PromptService
from ..engine.sse_bus import sse_format
from ..models import Report
from .deps import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ai", tags=["AI解读"])


class InterpretIn(BaseModel):
    question: str = ""
    data: dict = Field(default_factory=dict)
    prompt_template_id: int | None = None
    model_id: int | None = None
    report_id: int | None = None


def normalize_dashboard_data(data: dict) -> dict:
    """把任意问数结果规整为 AiInterpreter 需要的 {cards:[{title, data:{columns, rows}}]}。

    - dashboard 快照：已含 cards 列表，原样返回
    - 单查询结果：{columns, rows} → 包成一张「查询结果」卡
    - 其他任意 dict：包成一张「数据」卡
    """
    if not data:
        return {"cards": []}
    cards = data.get("cards")
    if isinstance(cards, list):
        return data
    columns = data.get("columns")
    rows = data.get("rows")
    if isinstance(columns, list) and isinstance(rows, list):
        return {"cards": [{"title": str(data.get("title") or "查询结果"),
                           "data": {"columns": columns, "rows": rows}}]}
    return {"cards": [{"title": str(data.get("title") or "数据"), "data": data}]}


@router.post("/interpret")
def interpret(body: InterpretIn, db: Session = Depends(get_db),
              user=Depends(get_current_user)):
    """按需 AI 解读（SSE 流式，事件同主链路 ai_interpretation_*）。"""
    if not body.data:
        raise HTTPException(400, "缺少解读数据")

    # 指定提示词时校验存在性（不指定则使用场景默认提示词）
    if body.prompt_template_id:
        tpl = PromptService(db, workspace_id=user.workspace_id).get(body.prompt_template_id)
        if not tpl:
            raise HTTPException(400, "提示词模板不存在")

    # 报告模式：前置归属校验（写回发生在流内）
    report = None
    if body.report_id:
        report = db.query(Report).filter(
            Report.id == body.report_id, Report.is_deleted.is_(False)).first()
        if not report or report.workspace_id != user.workspace_id:
            raise HTTPException(404, "报告不存在")

    dashboard_data = normalize_dashboard_data(body.data)

    def gen() -> Generator[str, None, None]:
        s = SessionLocal()
        try:
            llm = None
            if body.model_id:
                llm = resolve_llm_client_by_id(s, body.model_id, user.workspace_id)
            else:
                llm = resolve_llm_client(s, user.workspace_id, scene="sql")
            if not llm or not llm.configured:
                yield sse_format("ai_interpretation_error",
                                 {"error": "LLM 未配置，请先在模型配置中添加可用模型"})
                yield sse_format("done", {})
                return

            interpreter = AiInterpreter(
                llm=llm,
                prompt_service=PromptService(s, workspace_id=user.workspace_id),
            )
            for event in interpreter.interpret_stream(
                body.question, dashboard_data,
                prompt_template_id=body.prompt_template_id,
            ):
                payload = {k: v for k, v in event.items() if k != "type"}
                yield sse_format(event["type"], payload)
                # 报告模式：解读完成后回写报告（在流内会话重新查询，避免跨会话写入不生效）
                if event["type"] == "ai_interpretation_done" and report:
                    try:
                        rep = s.query(Report).filter(Report.id == report.id).first()
                        if rep:
                            rep.ai_interpretation = event.get("result") or {}
                            rep.interpretation_text = event.get("raw_text") or ""
                            s.commit()
                            logger.info("AI 解读已回写报告 report_id=%s", report.id)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("报告回写解读失败 report_id=%s: %s", report.id, exc)
            yield sse_format("done", {})
        except Exception as exc:  # noqa: BLE001
            logger.exception("AI 解读接口异常")
            yield sse_format("ai_interpretation_error", {"error": str(exc)})
            yield sse_format("done", {})
        finally:
            s.close()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
