# -*- coding: utf-8 -*-
"""對話串的存取（V6）。

三個端點（rag/query、agent/chat、agent/route）本來各自寫一份 append 邏輯，
連 flag_modified 這種容易漏掉的細節都各寫一次。集中在這裡，避免三份實作漂移
—— 這與 section_path 那次的理由相同：同一件事有兩套規則，不一致只會在很久
以後才被發現。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from .. import models

logger = logging.getLogger(__name__)

DEFAULT_TITLE = "新對話"
_TITLE_MAX = 40


def make_title(question: str) -> str:
    """用第一個問題當標題。

    不呼叫 LLM 產生摘要標題：那會讓「送出第一個問題」多等好幾秒，
    而使用者當下最想看到的是答案。截斷的問題已經足夠辨識，
    而且使用者可以自己改名。
    """
    t = re.sub(r"\s+", " ", (question or "").strip())
    if not t:
        return DEFAULT_TITLE
    return t[:_TITLE_MAX] + ("…" if len(t) > _TITLE_MAX else "")


def list_for_user(db: Session, user_id: str) -> List[models.Conversation]:
    """側邊欄用：釘選優先，其餘依最後更新排序。"""
    return (
        db.query(models.Conversation)
        .filter(models.Conversation.user_id == user_id)
        .order_by(models.Conversation.is_pinned.desc(),
                  models.Conversation.updated_at.desc())
        .all()
    )


def get_owned(db: Session, user_id: str, conversation_id: str) -> Optional[models.Conversation]:
    """取對話，並確認屬於這位使用者。

    一律用 user_id 一起過濾，而不是先取出再比對 —— 少一個「忘記比對」
    就等於任何人都能讀別人的對話。
    """
    if not conversation_id:
        return None
    return (
        db.query(models.Conversation)
        .filter(models.Conversation.id == conversation_id,
                models.Conversation.user_id == user_id)
        .first()
    )


def create(db: Session, user_id: str, title: Optional[str] = None,
           messages: Optional[List[Dict[str, Any]]] = None) -> models.Conversation:
    row = models.Conversation(
        user_id=user_id,
        title=(title or DEFAULT_TITLE)[:200],
        messages=list(messages or []),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def append_message(db: Session, user_id: str, conversation_id: Optional[str],
                   question: str, answer: str,
                   sources: Optional[List[Dict[str, Any]]] = None,
                   mode: str = "rag",
                   optimized_query: Optional[str] = None,
                   is_followup: bool = False) -> Optional[models.Conversation]:
    """把一輪問答寫進對話串；conversation_id 為空或不屬於此人時自動開新的一條。

    回傳寫入的那條對話（呼叫端要把 id 回給前端，前端才知道這輪落在哪一串）。
    失敗一律回 None 並 rollback —— 存不進去不該讓整個回答失敗，
    使用者已經看到答案了。
    """
    entry = {
        "question": question,
        "answer": answer,
        # sources 必須持久化：重整頁面後從 DB 載回的訊息若沒有來源，
        # 前端的「預覽 / 儲存筆記」會整個消失（兩者都讀 msg.sources）。
        "sources": sources or [],
        "mode": mode,
        "agentMode": mode == "agent",
        # 「AI 理解」與「追問」標籤同樣要持久化：只活在當輪 SSE 事件裡的話，
        # 切換對話再切回來（訊息從 DB 重載）標籤就消失 —— 使用者會以為系統
        # 沒顯示「最終用什麼去查」。optimized_query 只在真的改寫時存。
        "optimized_query": optimized_query,
        "is_followup": bool(is_followup),
        "timestamp": datetime.utcnow().isoformat(),
    }
    try:
        row = get_owned(db, user_id, conversation_id) if conversation_id else None
        if row is None:
            row = models.Conversation(
                user_id=user_id,
                title=make_title(question),
                messages=[entry],
            )
            db.add(row)
        else:
            # MutableList 對「整個換掉」才可靠；append 有時不會標記 dirty，
            # 因此沿用「重新指派 + flag_modified」的寫法。
            row.messages = (row.messages or []) + [entry]
            flag_modified(row, "messages")
            if row.title == DEFAULT_TITLE and len(row.messages) == 1:
                row.title = make_title(question)
        db.commit()
        db.refresh(row)
        return row
    except Exception as exc:  # noqa: BLE001
        logger.warning("對話寫入失敗 conversation_id=%s: %s", conversation_id, exc)
        db.rollback()
        return None


def to_summary(row: models.Conversation) -> Dict[str, Any]:
    """側邊欄卡片要的最小資料 —— 不含 messages，列表不該把整包對話送出去。"""
    msgs = row.messages or []
    return {
        "id": row.id,
        "title": row.title,
        "is_pinned": bool(row.is_pinned),
        "message_count": len(msgs),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "preview": (msgs[-1].get("question") if msgs else "") or "",
    }
