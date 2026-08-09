"""
Agent chat endpoint — SSE streaming of ReAct steps.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ... import models, schemas
from ...core.security import get_current_user
from ...database import SessionLocal, get_db
from ...services import agent

logger = logging.getLogger(__name__)
router = APIRouter()


def _sse(event: str, data: Dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


@router.post("/chat")
def agent_chat(
    payload: schemas.AgentChatRequest,
    current_user=Depends(get_current_user),
):
    """
    Stream Agent reasoning + final answer as SSE.

    Note: we open a fresh DB session inside the generator so SQLAlchemy isn't
    pinned to a request scope that ends as soon as the generator starts.
    """
    question = (payload.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="問題不可為空")

    history = payload.conversation_history or []
    max_steps = payload.max_steps
    user_id = current_user.id

    def event_stream():
        db = SessionLocal()
        try:
            # 檢索範圍必須在任何檢索之前設定。原本 Agent 模式完全忽略前端的
            # 文件／資料夾選擇，使用者鎖定了某份文件仍會撈到別的規範。
            agent.set_retrieval_scope(
                db,
                document_id=payload.document_id,
                folder_ids=payload.folder_ids,
                classification_id=payload.classification_id,
                project_id=payload.project_id,
            )
            final_text = ""
            final_sources = []
            for evt in agent.run_agent(
                db,
                question,
                conversation_history=history,
                max_steps=max_steps,
            ):
                etype = evt.get("type")
                if etype == "final":
                    final_text = evt.get("text") or ""
                    final_sources = evt.get("sources") or []
                    yield _sse("final", {"text": final_text, "sources": final_sources})
                elif etype == "error":
                    yield _sse("error", {"message": evt.get("message")})
                else:
                    yield _sse(etype or "info", evt)

            # Save to UserConversation (mode=agent flag in payload so frontend
            # can render this thread separately if it wants)
            try:
                row = (
                    db.query(models.UserConversation)
                    .filter_by(user_id=user_id)
                    .first()
                )
                entry = {
                    "question": question,
                    "answer": final_text,
                    # 持久化 sources，否則重整頁面後從 DB 載回的訊息沒有來源
                    # → 前端「預覽 / 儲存筆記」消失（兩者都看 msg.sources）。
                    "sources": final_sources,
                    "mode": "agent",
                    "agentMode": True,
                    "timestamp": datetime.utcnow().isoformat(),
                }
                if row:
                    from sqlalchemy.orm.attributes import flag_modified
                    row.messages = (row.messages or []) + [entry]
                    flag_modified(row, "messages")
                else:
                    row = models.UserConversation(user_id=user_id, messages=[entry])
                    db.add(row)
                db.commit()
            except Exception as db_err:
                logger.warning("agent conversation save failed: %s", db_err)
                db.rollback()

            yield _sse("done", {"ok": True})
        except Exception as e:
            logger.error("agent stream failed: %s", e, exc_info=True)
            yield _sse("error", {"message": str(e)})
        finally:
            db.close()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/route")
def agent_route(
    payload: schemas.AgentChatRequest,
    current_user=Depends(get_current_user),
):
    """混合模式:自動判定問題類型 → 關係/列舉題走 Agent(KG 工具),純內容題走純 RAG。
    一律以 SSE 串流回應;先發一個 `route` 事件告知選了哪個模式,接著:
      - agent 模式:照常串流 thought/tool_call/observation/final
      - rag 模式:直接發一個 final(無工具步驟,較快)
    """
    question = (payload.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="問題不可為空")
    history = payload.conversation_history or []
    max_steps = payload.max_steps
    user_id = current_user.id
    mode = agent.route_mode(question)

    def event_stream():
        db = SessionLocal()
        try:
            agent.set_retrieval_scope(
                db,
                document_id=payload.document_id,
                folder_ids=payload.folder_ids,
                classification_id=payload.classification_id,
                project_id=payload.project_id,
            )
            yield _sse("route", {"mode": mode})
            final_text, final_sources = "", []
            if mode == "agent":
                for evt in agent.run_agent(db, question, conversation_history=history, max_steps=max_steps):
                    etype = evt.get("type")
                    if etype == "final":
                        final_text = evt.get("text") or ""
                        final_sources = evt.get("sources") or []
                        yield _sse("final", {"text": final_text, "sources": final_sources})
                    elif etype == "error":
                        yield _sse("error", {"message": evt.get("message")})
                    else:
                        yield _sse(etype or "info", evt)
            else:
                final_text, final_sources = agent.run_rag_only(db, question, conversation_history=history)
                yield _sse("final", {"text": final_text, "sources": final_sources})

            try:
                row = db.query(models.UserConversation).filter_by(user_id=user_id).first()
                entry = {"question": question, "answer": final_text, "sources": final_sources,
                         "mode": f"hybrid:{mode}", "agentMode": (mode == "agent"),
                         "timestamp": datetime.utcnow().isoformat()}
                if row:
                    from sqlalchemy.orm.attributes import flag_modified
                    row.messages = (row.messages or []) + [entry]
                    flag_modified(row, "messages")
                else:
                    db.add(models.UserConversation(user_id=user_id, messages=[entry]))
                db.commit()
            except Exception as db_err:
                logger.warning("hybrid conversation save failed: %s", db_err)
                db.rollback()

            yield _sse("done", {"ok": True, "mode": mode})
        except Exception as e:
            logger.error("hybrid route failed: %s", e, exc_info=True)
            yield _sse("error", {"message": str(e)})
        finally:
            db.close()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/tools")
def list_tools(current_user=Depends(get_current_user)):
    """For the frontend to show which tools the Agent has."""
    from ...services.agent_tools import TOOLS
    return {
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                "params": list(t.schema.get("properties", {}).keys()),
            }
            for t in TOOLS.values()
        ]
    }
