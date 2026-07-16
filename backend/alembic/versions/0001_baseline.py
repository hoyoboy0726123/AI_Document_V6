"""Baseline — idempotent schema bootstrap.

取代 main.py 啟動時的 `Base.metadata.create_all` + `ensure_schema_updates()` ad-hoc DDL
（audit M）。此 migration 對「全新 DB」與「既有 DB」都安全：
- create_all(checkfirst=True)：缺的表才建。
- 欄位補丁全部用 inspector 檢查後才 ALTER（與舊 ensure_schema_updates 相同的守衛式邏輯）。

之後的 schema 變更請寫成新的 alembic revision，不要再回頭改這支。

Revision ID: 0001_baseline
Revises:
Create Date: 2026-07-08
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    # 1) 建立所有缺少的表（checkfirst — 既有表不動）
    from app.database import Base
    from app import models  # noqa: F401  (確保 model 都註冊)
    Base.metadata.create_all(bind=bind, checkfirst=True)

    inspector = sa.inspect(bind)
    dialect = bind.dialect.name

    # 2) documents 欄位補丁（守衛式，port 自 ensure_schema_updates）
    doc_cols = {c["name"] for c in inspector.get_columns("documents")}
    if "pdf_path" not in doc_cols:
        op.add_column("documents", sa.Column("pdf_path", sa.String(512)))
    if "full_analysis" not in doc_cols:
        op.add_column("documents", sa.Column("full_analysis", sa.Text() if dialect == "sqlite" else sa.JSON()))
    if "ai_summary" not in doc_cols:
        op.add_column("documents", sa.Column("ai_summary", sa.Text()))
    if "is_image_based" not in doc_cols:
        op.add_column("documents", sa.Column("is_image_based", sa.Boolean(), nullable=False, server_default=sa.text("0")))
    if "ocr_status" not in doc_cols:
        op.add_column("documents", sa.Column("ocr_status", sa.String(20), nullable=False, server_default="not_needed"))
    if "ocr_method" not in doc_cols:
        op.add_column("documents", sa.Column("ocr_method", sa.String(50)))
    if "folder_id" not in doc_cols:
        op.add_column("documents", sa.Column("folder_id", sa.String(36)))

    # 3) document_chunks.faiss_id（含 backfill，port 自 ensure_schema_updates）
    if inspector.has_table("document_chunks"):
        chunk_cols = {c["name"] for c in inspector.get_columns("document_chunks")}
        if "faiss_id" not in chunk_cols:
            op.add_column("document_chunks", sa.Column("faiss_id", sa.BigInteger()))
            rows = bind.execute(sa.text("SELECT id FROM document_chunks")).fetchall()
            for index, (chunk_id,) in enumerate(rows, start=1):
                bind.execute(
                    sa.text("UPDATE document_chunks SET faiss_id = :f WHERE id = :cid"),
                    {"f": index, "cid": chunk_id},
                )

    # 4) background_tasks.result
    if inspector.has_table("background_tasks"):
        bg_cols = {c["name"] for c in inspector.get_columns("background_tasks")}
        if "result" not in bg_cols:
            op.add_column("background_tasks", sa.Column("result", sa.Text() if dialect == "sqlite" else sa.JSON()))

    # 5) document_notes.user_id
    if inspector.has_table("document_notes"):
        note_cols = {c["name"] for c in inspector.get_columns("document_notes")}
        if "user_id" not in note_cols:
            op.add_column("document_notes", sa.Column("user_id", sa.String(36)))


def downgrade() -> None:
    # baseline 不提供 downgrade（會摧毀資料）
    raise NotImplementedError("baseline migration cannot be downgraded")
