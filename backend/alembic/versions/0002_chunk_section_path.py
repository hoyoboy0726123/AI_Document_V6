"""document_chunks.section_path — chunk 所屬的章節（METHOD / ANNEX）。

用途是取代「±15 頁」這種近似，用真正的方法邊界判斷兩段證據是否屬於同一個
測試方法（見 agent.in_scope）。值由 scripts/backfill_section_path.py 從
kg_entities 既有的標題回填，不需要重新解析 PDF 或重新嵌入。

Revision ID: 0002_chunk_section_path
Revises: 0001_baseline
Create Date: 2026-08-08
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_chunk_section_path"
down_revision: Union[str, None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    # 守衛式：既有 dev DB 可能已由更早的 ad-hoc ALTER 加過這個欄位
    if not _has_column(bind, "document_chunks", "section_path"):
        op.add_column("document_chunks", sa.Column("section_path", sa.String(200), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "document_chunks", "section_path"):
        op.drop_column("document_chunks", "section_path")
