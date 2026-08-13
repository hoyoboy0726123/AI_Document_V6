from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field


# ---------- Authentication ----------


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TokenWithRefresh(BaseModel):
    """
    Authentication response with both access and refresh tokens.

    Used for login endpoint to provide long-term authentication.
    """
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # Access token expiration in seconds


class RefreshTokenRequest(BaseModel):
    """Request to refresh an access token using a refresh token."""
    refresh_token: str


class LogoutRequest(BaseModel):
    """Request to logout and revoke refresh token."""
    refresh_token: str


class TokenData(BaseModel):
    username: Optional[str] = None


class UserBase(BaseModel):
    username: str
    email: EmailStr


class UserCreate(UserBase):
    password: str = Field(min_length=8)
    role: Optional[str] = None


class UserRead(UserBase):
    id: str
    role: str
    is_active: bool
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


# ---------- Metadata ----------


class MetadataOptionBase(BaseModel):
    value: str
    display_value: str


class MetadataOptionCreate(MetadataOptionBase):
    order_index: int = 0


class MetadataOptionRead(MetadataOptionBase):
    id: str
    is_active: bool
    order_index: int
    model_config = ConfigDict(from_attributes=True)


class MetadataFieldBase(BaseModel):
    name: str
    display_name: str
    field_type: str
    is_required: bool = False
    description: Optional[str] = None
    order_index: int = 0


class MetadataFieldCreate(MetadataFieldBase):
    options: Optional[List[MetadataOptionCreate]] = None


class MetadataFieldUpdate(BaseModel):
    display_name: Optional[str] = None
    description: Optional[str] = None
    field_type: Optional[str] = None
    is_required: Optional[bool] = None
    is_active: Optional[bool] = None
    order_index: Optional[int] = None


class MetadataFieldRead(MetadataFieldBase):
    id: str
    is_active: bool
    options: List[MetadataOptionRead] = Field(default_factory=list)
    model_config = ConfigDict(from_attributes=True)


# ---------- Documents ----------


class DocumentBase(BaseModel):
    title: str
    content: Optional[str] = None


class DocumentCreate(DocumentBase):
    metadata: Dict[str, Any] = Field(default_factory=dict)
    classification_id: Optional[str] = None
    source_pdf_path: Optional[str] = None
    segments: Optional[List[Dict[str, Any]]] = None  # 預先提取的 PDF segments，避免重複處理
    ai_summary: Optional[str] = None  # AI 自動生成的文件摘要
    is_image_based: bool = False  # 是否為圖片型 PDF
    original_filename: Optional[str] = None  # 原始檔案名稱
    force_vision: bool = False  # 建立時用 VL 視覺模型重新解析（在向量化階段執行）
    force_ocr: bool = False  # 建立時強制走 PaddleOCR（即使是文字型 PDF）


class DocumentUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    classification_id: Optional[str] = None
    source_pdf_path: Optional[str] = None
    ai_summary: Optional[str] = None
    folder_id: Optional[str] = None  # "__unset__" = remove from folder


class ClassificationSummary(BaseModel):
    id: str
    name: str
    code: Optional[str] = None
    description: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)


class ClassificationCreate(BaseModel):
    name: str
    code: Optional[str] = None
    description: Optional[str] = None


class ClassificationUpdate(BaseModel):
    name: Optional[str] = None
    code: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


class ClassificationRead(BaseModel):
    id: str
    name: str
    code: Optional[str] = None
    description: Optional[str] = None
    is_active: bool
    created_at: datetime
    updated_at: Optional[datetime] = None
    model_config = ConfigDict(from_attributes=True)


class FolderCreate(BaseModel):
    name: str
    parent_id: Optional[str] = None
    order_index: int = 0


class FolderUpdate(BaseModel):
    name: Optional[str] = None
    parent_id: Optional[str] = None  # "__root__" = move to top level
    order_index: Optional[int] = None


class FolderRead(BaseModel):
    id: str
    name: str
    parent_id: Optional[str] = None
    order_index: int
    created_at: datetime
    doc_count: int = 0
    model_config = ConfigDict(from_attributes=True)


class DocumentRead(DocumentBase):
    id: str
    creator_id: str
    classification_id: Optional[str] = None
    is_archived: bool
    created_at: datetime
    updated_at: Optional[datetime] = None
    metadata_data: Dict[str, Any] = Field(default_factory=dict, serialization_alias="metadata")
    classification: Optional[ClassificationSummary] = None
    pdf_path: Optional[str] = None
    ai_summary: Optional[str] = None  # AI 自動生成的文件摘要
    is_image_based: bool = False  # 是否為圖片型PDF
    ocr_status: str = "not_needed"  # OCR處理狀態
    ocr_method: Optional[str] = None  # OCR方法
    full_analysis: Optional[Dict[str, Any]] = None  # 整份文件 AI 分析結果（包含初始分析和對話歷史）
    task_id: Optional[str] = None  # 背景任務 ID（VL 解析時使用）
    folder_id: Optional[str] = None  # 所屬資料夾 ID
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class DocumentListResponse(BaseModel):
    items: List[DocumentRead]
    total: int
    page: int
    page_size: int


class DocumentMetadataUpdate(BaseModel):
    metadata: Optional[Dict[str, Any]] = None
    add_keywords: Optional[List[str]] = None
    remove_keywords: Optional[List[str]] = None


class DocumentClassificationApply(BaseModel):
    classification_id: str


class AISuggestion(BaseModel):
    summary: Optional[str] = None
    classification: Optional[str] = None
    classification_is_new: bool = False
    classification_reason: Optional[str] = None
    project: Optional[str] = None
    project_is_new: bool = False
    project_reason: Optional[str] = None
    keywords: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class DocumentSegment(BaseModel):
    page: int
    paragraph_index: int
    text: str


class DocumentUploadResponse(BaseModel):
    text: str
    filename: str
    segments: List[DocumentSegment] = Field(default_factory=list)
    suggested_metadata: Dict[str, Any] = Field(default_factory=dict)
    suggestion: AISuggestion = Field(default_factory=AISuggestion)
    pdf_temp_path: Optional[str] = None
    is_image_based: bool = False  # 是否為圖片型PDF，需要OCR
    total_pages: Optional[int] = None  # PDF總頁數


class SuggestionClassificationCreate(BaseModel):
    name: str
    description: Optional[str] = None


class SuggestionProjectCreate(BaseModel):
    display_name: str
    description: Optional[str] = None


class SuggestionAcceptanceRequest(BaseModel):
    classification: Optional[SuggestionClassificationCreate] = None
    project: Optional[SuggestionProjectCreate] = None


class SuggestionAcceptanceResponse(BaseModel):
    classification: Optional[ClassificationSummary] = None
    project_option: Optional[MetadataOptionRead] = None


class DocumentChunkSource(BaseModel):
    document_id: str
    title: str
    page: Optional[int] = None
    snippet: str
    # None = 純關鍵字命中（無向量分數），前端顯示「關鍵字命中」而非 0.000
    score: Optional[float] = None


class ConversationMessage(BaseModel):
    """對話訊息"""
    question: str
    answer: str
    sources: List[DocumentChunkSource] = Field(default_factory=list)


class RAGPromptRead(BaseModel):
    system_prompt: str
    user_template: str
    is_default: bool


class RAGPromptUpdate(BaseModel):
    system_prompt: Optional[str] = None
    user_template: Optional[str] = None


class RAGQueryRequest(BaseModel):
    question: str
    top_k: int = Field(default=5, ge=1, le=10)
    classification_id: Optional[str] = None
    project_id: Optional[str] = None
    document_id: Optional[str] = None
    folder_ids: Optional[List[str]] = None  # 資料夾範圍過濾（含所有子資料夾）
    # 對話歷史（用於追問）
    conversation_history: List[ConversationMessage] = Field(default_factory=list)
    # 當找不到相關文件時，是否使用 AI 自身知識回答
    use_ai_fallback: bool = False
    # 是否跳過 AI 理解（用於新問題輸入，不進行 AI 優化）
    skip_ai_understanding: bool = False
    # 這輪要寫進哪一條對話串（V6）。留空代表開新的一條，
    # done 事件會回傳實際的 conversation_id。
    conversation_id: Optional[str] = None


class RAGQueryResponse(BaseModel):
    answer: str
    sources: List[DocumentChunkSource]
    # 是否為追問（需要重新搜尋）
    is_followup: bool = False
    # 優化後的查詢關鍵字（追問時）
    optimized_query: Optional[str] = None
    # 相關問題建議
    suggested_questions: List[str] = Field(default_factory=list)
    # 是否使用了 AI 備援模式（非文件查詢）
    used_ai_fallback: bool = False
    # 檢索信心偏低（最相關段落 cross-encoder 分數低於門檻）：answer 已改為「最接近內容＋修正建議」
    low_confidence: bool = False


class MetadataOptionUpdate(BaseModel):
    display_value: Optional[str] = None
    is_active: Optional[bool] = None
    order_index: Optional[int] = None


class PdfPageAnalysisMessage(BaseModel):
    """PDF 頁面分析對話訊息"""
    question: str
    answer: str


class PdfPageAnalysisRequest(BaseModel):
    """PDF 頁面分析請求"""
    document_id: str
    page_numbers: List[int] = Field(..., min_items=1, max_items=10)  # 最多 10 頁
    question: Optional[str] = None
    conversation_history: List[PdfPageAnalysisMessage] = Field(default_factory=list)


class PdfPageAnalysisResponse(BaseModel):
    """PDF 頁面分析回應"""
    answer: str
    page_numbers: List[int]
    document_title: str


class TextSearchMatch(BaseModel):
    """文字搜尋匹配結果"""
    page: int
    paragraph_index: int
    text: str  # 完整文本（內部使用）
    snippet: str  # 關鍵字前後約100字的摘錄
    matched_text: str  # 高亮的匹配文字


class TextSearchResponse(BaseModel):
    """文字搜尋回應"""
    query: str
    total_matches: int
    matches: List[TextSearchMatch]


class CrossDocumentSearchMatch(BaseModel):
    """跨文件搜尋匹配結果"""
    document_id: str
    document_title: str
    page: int
    paragraph_index: int
    text: str  # 完整文本
    snippet: str  # 關鍵字前後約100字的摘錄
    matched_text: str  # 高亮的匹配文字


class CrossDocumentSearchResponse(BaseModel):
    """跨文件搜尋回應"""
    query: str
    total_matches: int
    total_documents: int  # 命中的文件數量
    matches: List[CrossDocumentSearchMatch]


# ===== 系統配置 =====
class SystemConfigRead(BaseModel):
    """系統配置讀取"""
    embedding_model: str
    llm_model: str
    vision_model: Optional[str] = None
    available_models: List[str]
    ollama_version: Optional[str] = None
    total_documents: int
    total_chunks: int
    faiss_index_exists: bool
    vector_config: Optional[Dict[str, Any]] = None


class VectorConfigUpdate(BaseModel):
    """向量配置更新請求"""
    overlap_chars: int = Field(ge=0, description="向量塊重疊字符數（0 表示取消）")
    max_chars: int = Field(gt=0, description="向量塊最大字符數")
    min_similarity_score: float = Field(ge=0, le=1, description="向量匹配閾值")
    default_top_k: int = Field(gt=0, le=20, description="預設返回來源數量")
    search_multiplier: int = Field(gt=0, description="搜索倍數")


class EmbeddingModelUpdateRequest(BaseModel):
    """更新 embedding 模型請求"""
    model_name: str  # 新的模型名稱


class ReVectorizeRequest(BaseModel):
    """重新向量化請求"""
    confirm: bool = False  # 確認執行（防止誤操作）


class ReVectorizeResponse(BaseModel):
    """重新向量化回應"""
    success: bool
    message: str
    processed_documents: int
    processed_chunks: int
    new_model: str


class OCRProcessRequest(BaseModel):
    """OCR 處理請求"""
    mode: str  # "immediate", "background", "skip"
    document_id: Optional[str] = None  # 用於後續處理已存在的文件


class OCRStatusResponse(BaseModel):
    """OCR 狀態回應"""
    document_id: str
    ocr_status: str
    is_image_based: bool
    ocr_method: Optional[str] = None
    progress: Optional[int] = None  # 0-100 進度百分比
    message: Optional[str] = None


class DocumentNoteBase(BaseModel):
    question: str
    answer: str


class DocumentNoteCreate(DocumentNoteBase):
    pass


class DocumentNoteUpdate(BaseModel):
    question: Optional[str] = None
    answer: Optional[str] = None


class DocumentNoteRead(DocumentNoteBase):
    id: str
    document_id: str
    created_at: datetime
    updated_at: Optional[datetime] = None
    model_config = ConfigDict(from_attributes=True)


class GlobalNoteRead(DocumentNoteBase):
    """跨文件筆記（含所屬文件資訊）"""
    id: str
    document_id: str
    document_title: str
    created_at: datetime
    updated_at: Optional[datetime] = None
    model_config = ConfigDict(from_attributes=True)


DocumentRead.model_rebuild()


# ── 向量塊管理 ────────────────────────────────────────────────────────────────

class ChunkRead(BaseModel):
    id: str
    chunk_index: int
    page: Optional[int] = None
    paragraph_index: Optional[int] = None
    text: str
    char_count: int
    faiss_id: int
    model_config = ConfigDict(from_attributes=True)


class ChunkCreate(BaseModel):
    page: Optional[int] = None
    text: str


class ChunkUpdate(BaseModel):
    text: str


class ChunkListResponse(BaseModel):
    items: List[ChunkRead]
    total: int
    total_chars: int
    avg_chars: int


class ChunkMergeRequest(BaseModel):
    chunk_ids: List[str]  # 要合併的 chunk id 列表，按順序合併


class ChunkSplitRequest(BaseModel):
    split_at: int  # 字元位置


# ── 向量查詢測試 ──────────────────────────────────────────────────────────────

class VectorSearchTestRequest(BaseModel):
    query: str
    top_k: int = Field(default=5, ge=1, le=20)
    min_score: float = Field(default=0.3, ge=0.0, le=1.0)
    document_id: Optional[str] = None


class VectorSearchTestResult(BaseModel):
    rank: int
    chunk_id: str
    document_id: str
    document_title: str
    page: Optional[int] = None
    score: float
    text: str


class VectorSearchTestResponse(BaseModel):
    query: str
    results: List[VectorSearchTestResult]
    elapsed_ms: int


# ── 向量庫健康儀表板 ──────────────────────────────────────────────────────────

class DocumentChunkStat(BaseModel):
    document_id: str
    document_title: str
    chunk_count: int
    total_chars: int
    avg_chars: int
    empty_embedding_count: int


class AbnormalChunk(BaseModel):
    chunk_id: str
    document_id: str
    document_title: str
    page: Optional[int] = None
    char_count: int
    reason: str  # "too_short" | "too_long"
    text_preview: str


class VectorHealthResponse(BaseModel):
    total_chunks: int
    total_documents: int
    total_chars: int
    avg_chars_per_chunk: int
    empty_embedding_count: int
    abnormal_chunks: List[AbnormalChunk]
    document_stats: List[DocumentChunkStat]


# ── 背景任務 ──────────────────────────────────────────────────────────────────

class TaskRead(BaseModel):
    id: str
    task_type: str
    status: str  # pending / running / completed / failed
    progress: int
    message: Optional[str] = None
    document_id: Optional[str] = None
    error: Optional[str] = None
    result: Optional[Dict[str, Any]] = None  # pdf_analyze 任務完成後的分析結果
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


# ── Knowledge Graph ──────────────────────────────────────────────────────────


class KGEntityRead(BaseModel):
    id: str
    canonical_id: str
    name: str
    type: str
    description: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)


class KGRelationRead(BaseModel):
    id: str
    src_id: str
    dst_id: str
    rel_type: str
    document_id: Optional[str] = None
    chunk_id: Optional[str] = None
    confidence: float
    evidence: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)


class KGEntityNeighbor(BaseModel):
    entity: KGEntityRead
    rel_type: str
    direction: str   # "out" (src→this neighbor) | "in" (neighbor→src)
    confidence: float
    document_id: Optional[str] = None


class KGNeighborsResponse(BaseModel):
    center: KGEntityRead
    neighbors: List[KGEntityNeighbor]


class KGGraphNode(BaseModel):
    id: str
    canonical_id: str
    name: str
    type: str


class KGGraphEdge(BaseModel):
    src_id: str
    dst_id: str
    rel_type: str
    confidence: float


class KGGraphResponse(BaseModel):
    nodes: List[KGGraphNode]
    edges: List[KGGraphEdge]


# ── Agent ────────────────────────────────────────────────────────────────────


class AgentChatRequest(BaseModel):
    question: str
    conversation_history: List[Dict[str, Any]] = Field(default_factory=list)
    max_steps: int = Field(default=8, ge=1, le=15)
    top_k: int = Field(default=5, ge=1, le=10)
    # 檢索範圍。原本這四個欄位只有 RAGQueryRequest 有，Agent 與混合模式收不到，
    # 於是使用者在左側鎖定了某份文件，答案卻引用完全不相干的其他規範 ——
    # 介面看起來限定了範圍，實際上整個資料庫都在查。
    classification_id: Optional[str] = None
    project_id: Optional[str] = None
    document_id: Optional[str] = None
    folder_ids: Optional[List[str]] = None
    # 這輪要寫進哪一條對話串（V6）。留空代表開新的一條，
    # 後端會在 done 事件回傳實際的 conversation_id 讓前端接上。
    conversation_id: Optional[str] = None
