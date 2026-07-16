import uuid
from typing import List, Optional

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from .database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class TimestampMixin:
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[Optional[DateTime]] = mapped_column(DateTime(timezone=True), onupdate=func.now())


class BaseMixin(TimestampMixin):
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)


class User(BaseMixin, Base):
    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="assistant")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[Optional[DateTime]] = mapped_column(DateTime(timezone=True))

    documents: Mapped[List["Document"]] = relationship("Document", back_populates="creator")
    created_metadata_fields: Mapped[List["MetadataField"]] = relationship(
        "MetadataField", foreign_keys="MetadataField.created_by_id", back_populates="created_by"
    )
    updated_metadata_fields: Mapped[List["MetadataField"]] = relationship(
        "MetadataField", foreign_keys="MetadataField.updated_by_id", back_populates="updated_by"
    )
    refresh_tokens: Mapped[List["RefreshToken"]] = relationship(
        "RefreshToken", back_populates="user", cascade="all, delete-orphan"
    )


class RefreshToken(BaseMixin, Base):
    """
    Refresh token for long-term authentication without re-login.

    Security features:
    - Stored in database (can be revoked)
    - One-time use (token is deleted after refresh)
    - Expires after REFRESH_TOKEN_EXPIRE_DAYS
    - Bound to specific user
    """
    __tablename__ = "refresh_tokens"

    token: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    expires_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_revoked: Mapped[bool] = mapped_column(Boolean, default=False)

    # Optional: Track device/IP for security
    device_info: Mapped[Optional[str]] = mapped_column(String(255))
    ip_address: Mapped[Optional[str]] = mapped_column(String(45))  # IPv6 compatible

    user: Mapped["User"] = relationship("User", back_populates="refresh_tokens")


class ClassificationCategory(BaseMixin, Base):
    __tablename__ = "classification_categories"

    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    code: Mapped[Optional[str]] = mapped_column(String(64), unique=True)
    description: Mapped[Optional[str]] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    documents: Mapped[List["Document"]] = relationship("Document", back_populates="classification")


class Folder(BaseMixin, Base):
    __tablename__ = "folders"

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    parent_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("folders.id"), nullable=True, index=True
    )
    order_index: Mapped[int] = mapped_column(Integer, default=0)

    documents: Mapped[List["Document"]] = relationship("Document", back_populates="folder")


class Document(BaseMixin, Base):
    __tablename__ = "documents"

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[Optional[str]] = mapped_column(Text)
    creator_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    metadata_data: Mapped[dict] = mapped_column(MutableDict.as_mutable(JSON), default=dict, nullable=False)
    # Performance: Added index for frequent classification queries
    classification_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("classification_categories.id"),
        index=True
    )
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    pdf_path: Mapped[Optional[str]] = mapped_column(String(512))
    # AI 自動生成的文件摘要（在創建文件時由 AI 建議生成）
    ai_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 整份文件 AI 分析結果（包含初始分析和對話歷史）
    full_analysis: Mapped[Optional[dict]] = mapped_column(MutableDict.as_mutable(JSON), default=None, nullable=True)
    # OCR 相關欄位：用於處理圖片型PDF（掃描件、傳真件）
    is_image_based: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ocr_status: Mapped[str] = mapped_column(
        String(20),
        default="not_needed",
        nullable=False
    )  # not_needed, pending, processing, completed, failed, skipped
    ocr_method: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)  # gemini_vision
    folder_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("folders.id"), nullable=True, index=True
    )

    creator: Mapped[User] = relationship("User", back_populates="documents")
    folder: Mapped[Optional["Folder"]] = relationship("Folder", back_populates="documents")
    classification: Mapped[Optional[ClassificationCategory]] = relationship(
        "ClassificationCategory", back_populates="documents"
    )
    chunks: Mapped[List["DocumentChunk"]] = relationship(
        "DocumentChunk", back_populates="document", cascade="all, delete-orphan"
    )
    notes: Mapped[List["DocumentNote"]] = relationship(
        "DocumentNote", back_populates="document", cascade="all, delete-orphan"
    )


class MetadataField(BaseMixin, Base):
    __tablename__ = "metadata_fields"
    __table_args__ = (UniqueConstraint("name", name="uq_metadata_field_name"),)

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    field_type: Mapped[str] = mapped_column(String(32), nullable=False)
    is_required: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    created_by_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"))
    updated_by_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"))

    options: Mapped[List["MetadataOption"]] = relationship(
        "MetadataOption",
        back_populates="field",
        cascade="all, delete-orphan",
        order_by="MetadataOption.order_index"
    )
    created_by: Mapped[Optional[User]] = relationship(
        "User", foreign_keys=[created_by_id], back_populates="created_metadata_fields"
    )
    updated_by: Mapped[Optional[User]] = relationship(
        "User", foreign_keys=[updated_by_id], back_populates="updated_metadata_fields"
    )
    templates: Mapped[List["MetadataTemplateField"]] = relationship(
        "MetadataTemplateField", back_populates="field", cascade="all, delete-orphan"
    )


class MetadataOption(BaseMixin, Base):
    __tablename__ = "metadata_options"

    field_id: Mapped[str] = mapped_column(String(36), ForeignKey("metadata_fields.id"), nullable=False)
    value: Mapped[str] = mapped_column(String(128), nullable=False)
    display_value: Mapped[str] = mapped_column(String(128), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    order_index: Mapped[int] = mapped_column(Integer, default=0)

    field: Mapped[MetadataField] = relationship("MetadataField", back_populates="options")


class MetadataTemplate(BaseMixin, Base):
    __tablename__ = "metadata_templates"

    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"))

    fields: Mapped[List["MetadataTemplateField"]] = relationship(
        "MetadataTemplateField", back_populates="template", cascade="all, delete-orphan"
    )


class MetadataTemplateField(BaseMixin, Base):
    __tablename__ = "metadata_template_fields"
    __table_args__ = (
        UniqueConstraint("template_id", "field_id", name="uq_template_field"),
    )

    template_id: Mapped[str] = mapped_column(String(36), ForeignKey("metadata_templates.id"), nullable=False)
    field_id: Mapped[str] = mapped_column(String(36), ForeignKey("metadata_fields.id"), nullable=False)
    is_required_override: Mapped[Optional[bool]] = mapped_column(Boolean)
    order_index: Mapped[int] = mapped_column(Integer, default=0)

    template: Mapped[MetadataTemplate] = relationship("MetadataTemplate", back_populates="fields")
    field: Mapped[MetadataField] = relationship("MetadataField", back_populates="templates")


class DocumentChunk(BaseMixin, Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        # Performance: Composite index for ordering chunks within a document
        Index('idx_chunk_ordering', 'document_id', 'page', 'paragraph_index'),
    )

    document_id: Mapped[str] = mapped_column(String(36), ForeignKey("documents.id"), nullable=False, index=True)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    page: Mapped[Optional[int]] = mapped_column(Integer)
    paragraph_index: Mapped[Optional[int]] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[List[float]] = mapped_column(MutableList.as_mutable(JSON), default=list, nullable=False)
    # BigInteger: faiss_id 是 uuid4 % 2^63，SQLite 沒事但 PostgreSQL 的 INTEGER 是 32-bit
    # 會 NumericValueOutOfRange（audit H6）。BigInteger 在 SQLite 仍映射為 INTEGER，相容。
    faiss_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)

    document: Mapped[Document] = relationship("Document", back_populates="chunks")


class BackgroundTask(BaseMixin, Base):
    """非同步背景工作（VL 解析、批次向量化等）"""
    __tablename__ = "background_tasks"

    task_type: Mapped[str] = mapped_column(String(50), nullable=False)   # "vl_vectorize" / "vectorize" / "pdf_analyze"
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)  # pending/running/completed/failed
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # 0-100
    message: Mapped[Optional[str]] = mapped_column(Text)
    document_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("documents.id"))
    creator_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    error: Mapped[Optional[str]] = mapped_column(Text)
    result: Mapped[Optional[dict]] = mapped_column(MutableDict.as_mutable(JSON))  # pdf_analyze 任務的分析結果


class AuditLog(BaseMixin, Base):
    __tablename__ = "audit_logs"

    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    before_snapshot: Mapped[Optional[dict]] = mapped_column(MutableDict.as_mutable(JSON))
    after_snapshot: Mapped[Optional[dict]] = mapped_column(MutableDict.as_mutable(JSON))
    performed_by_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"))


class SystemConfig(Base):
    """系統配置表（鍵值存儲）"""
    __tablename__ = "system_configs"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(255))
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class DocumentUserAnalysis(Base):
    """Per-user, per-document PDF analysis conversation history"""
    __tablename__ = "document_user_analyses"

    document_id: Mapped[str] = mapped_column(String(36), ForeignKey("documents.id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), primary_key=True)
    messages: Mapped[list] = mapped_column(MutableList.as_mutable(JSON), default=list, nullable=False)
    updated_at: Mapped[Optional[DateTime]] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    document: Mapped["Document"] = relationship("Document")
    user: Mapped["User"] = relationship("User")


class UserConversation(Base):
    """Per-user QA conversation history (replaces localStorage)"""
    __tablename__ = "user_conversations"

    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), primary_key=True)
    messages: Mapped[list] = mapped_column(MutableList.as_mutable(JSON), default=list, nullable=False)
    updated_at: Mapped[Optional[DateTime]] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user: Mapped["User"] = relationship("User")


class DocumentNote(BaseMixin, Base):
    """
    User saved notes from AI conversation.
    """
    __tablename__ = "document_notes"

    document_id: Mapped[str] = mapped_column(String(36), ForeignKey("documents.id"), nullable=False, index=True)
    user_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)

    document: Mapped[Document] = relationship("Document", back_populates="notes")
    user: Mapped[Optional[User]] = relationship("User")


class KGEntity(BaseMixin, Base):
    """
    Knowledge graph node — a canonicalized spec identifier or term
    (e.g. "ISO 9001:2015", "MIL-STD-810G", "IEC 60068-2-30").
    """
    __tablename__ = "kg_entities"
    __table_args__ = (
        UniqueConstraint("canonical_id", name="uq_kg_entity_canonical"),
        Index("idx_kg_entity_type", "type"),
    )

    canonical_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(MutableDict.as_mutable(JSON), default=dict, nullable=False)

    src_relations: Mapped[List["KGRelation"]] = relationship(
        "KGRelation", foreign_keys="KGRelation.src_id", back_populates="src", cascade="all, delete-orphan"
    )
    dst_relations: Mapped[List["KGRelation"]] = relationship(
        "KGRelation", foreign_keys="KGRelation.dst_id", back_populates="dst", cascade="all, delete-orphan"
    )


class KGRelation(BaseMixin, Base):
    """
    Knowledge graph edge between two entities. rel_type enum:
    references | supersedes | defines | requires | derives_from
    """
    __tablename__ = "kg_relations"
    __table_args__ = (
        Index("idx_kg_relation_src", "src_id"),
        Index("idx_kg_relation_dst", "dst_id"),
        Index("idx_kg_relation_type", "rel_type"),
        UniqueConstraint("src_id", "dst_id", "rel_type", "document_id", name="uq_kg_relation_triple_doc"),
    )

    src_id: Mapped[str] = mapped_column(String(36), ForeignKey("kg_entities.id"), nullable=False)
    dst_id: Mapped[str] = mapped_column(String(36), ForeignKey("kg_entities.id"), nullable=False)
    rel_type: Mapped[str] = mapped_column(String(32), nullable=False)
    document_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("documents.id"), index=True)
    chunk_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("document_chunks.id"))
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    evidence: Mapped[Optional[str]] = mapped_column(Text)

    src: Mapped["KGEntity"] = relationship("KGEntity", foreign_keys=[src_id], back_populates="src_relations")
    dst: Mapped["KGEntity"] = relationship("KGEntity", foreign_keys=[dst_id], back_populates="dst_relations")
