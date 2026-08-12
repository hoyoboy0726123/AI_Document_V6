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
from ...services import agent, conversations

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
    conversation_id = payload.conversation_id

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

            # 寫進對話串。conversation_id 為空時 append_message 會自動開一條，
            # 並把 id 回給前端 —— 前端要靠它知道這輪落在哪一串上。
            saved = conversations.append_message(
                db, user_id, conversation_id,
                question=question, answer=final_text,
                sources=final_sources, mode="agent",
            )
            yield _sse("done", {"ok": True,
                                "conversation_id": saved.id if saved else None})
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
    conversation_id = payload.conversation_id
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
            meta: Dict[str, Any] = {}    # rag 分支填入改寫結果；agent 分支保持空
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
                # meta（宣告於分支前）會被填入查詢改寫的結果，交給前端顯示
                # 「AI 理解：xxx」並隨 append_message 持久化。
                final_text, final_sources = agent.run_rag_only(
                    db, question, conversation_history=history, out_meta=meta)
                yield _sse("final", {
                    "text": final_text,
                    "sources": final_sources,
                    "optimized_query": meta.get("optimized_query"),
                    "rewritten": meta.get("rewritten", False),
                })

            saved = conversations.append_message(
                db, user_id, conversation_id,
                question=question, answer=final_text,
                sources=final_sources, mode=f"hybrid:{mode}",
                optimized_query=(meta.get("optimized_query")
                                 if mode == "rag" and meta.get("rewritten") else None),
                is_followup=bool(history),
            )
            yield _sse("done", {"ok": True, "mode": mode,
                                "conversation_id": saved.id if saved else None})
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
