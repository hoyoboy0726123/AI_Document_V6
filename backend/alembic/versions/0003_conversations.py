"""conversations — 多對話串（V6）

UserConversation 的主鍵就是 user_id，結構上每人只能有一條扁平清單
（實測既有資料就是 148 則訊息全部堆在同一條裡）。要做 ChatGPT 式的
對話串，主鍵必須是對話自己的 id。

既有資料的處理：把每位使用者原本那一條整包搬成「一條對話」，標題固定為
「舊對話（V5 匯入）」。不做自動分割 —— 沒有任何可靠訊號能判斷 148 則訊息
之間哪裡該斷開，猜錯會把有脈絡的追問拆散。使用者之後可以自己改標題或刪除。

刻意不刪除 user_conversations 表：搬遷萬一有問題還能回頭對照。
確認 V6 穩定後再開一個 migration 清掉。

Revision ID: 0003_conversations
Revises: 0002_chunk_section_path
Create Date: 2026-08-10
"""
from typing import Sequence, Union
import json
import uuid

import sqlalchemy as sa
from alembic import op

revision: str = "0003_conversations"
down_revision: Union[str, None] = "0002_chunk_section_path"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_LEGACY_TITLE = "舊對話（V5 匯入）"


def _has_table(bind, name: str) -> bool:
    return name in sa.inspect(bind).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_table(bind, "conversations"):
        op.create_table(
            "conversations",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("title", sa.String(200), nullable=False, server_default="新對話"),
            sa.Column("messages", sa.JSON(), nullable=False),
            sa.Column("is_pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_conversations_user_id", "conversations", ["user_id"])
        op.create_index("idx_conversation_user_updated", "conversations", ["user_id", "updated_at"])

    # ── 搬遷既有資料 ──────────────────────────────────────────
    if not _has_table(bind, "user_conversations"):
        return

    # 已經搬過就不要重複（migration 必須可重複執行）
    already = bind.execute(sa.text(
        "SELECT COUNT(*) FROM conversations WHERE title = :t"
    ), {"t": _LEGACY_TITLE}).scalar()
    if already:
        return

    rows = bind.execute(sa.text(
        "SELECT user_id, messages, updated_at FROM user_conversations"
    )).fetchall()
    for user_id, messages, updated_at in rows:
        if isinstance(messages, str):
            try:
                messages = json.loads(messages)
            except (TypeError, ValueError):
                messages = []
        if not messages:
            continue
        bind.execute(sa.text(
            "INSERT INTO conversations (id, user_id, title, messages, is_pinned, created_at, updated_at) "
            "VALUES (:id, :uid, :title, :msgs, :pin, :created, :updated)"
        ), {
            "id": str(uuid.uuid4()),
            "uid": user_id,
            "title": _LEGACY_TITLE,
            "msgs": json.dumps(messages, ensure_ascii=False),
            "pin": False,
            "created": updated_at,
            "updated": updated_at,
        })


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "conversations"):
        op.drop_table("conversations")
