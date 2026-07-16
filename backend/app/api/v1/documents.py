import logging
import re
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import func
from sqlalchemy.orm import Session
from pdfminer.pdfdocument import PDFPasswordIncorrect

from ... import models, schemas
from ...core.config import settings
from ...core.security import get_current_user, get_current_admin_user
from ...database import get_db
from ...services import ai
from ...services.documents import (
    DocumentService,
    run_document_finalize_task,
    run_document_reocr_task,
    run_upload_analysis_task,
)
from ...services.metadata import MetadataService
from ...services.pdf_processing import extract_text_and_segments, suggest_keywords
from ...services.pdf_image import get_pdf_page_count
from ...utils.security import validate_file_path
from ...utils.logging_config import get_logger
from ...utils.exceptions import ResourceNotFoundError, ValidationError
from ...utils.validators import ClassificationValidator, FileValidator

logger = get_logger(__name__)

router = APIRouter()


def _doc_service(db: Session) -> DocumentService:
    return DocumentService(db)


# 會「重建該文件 chunks / 向量」的任務類型 —— 同一文件同時只允許一個在跑
_DOC_MUTATING_TASKS = ("reocr", "vectorize", "vl_vectorize", "ocr_vectorize")


def _inflight_doc_task(db: Session, document_id: str):
    """回傳該文件目前 pending/running 的重跑/向量化任務（沒有則 None）。

    用來做「同文件去重」：避免使用者重複點擊「高精度重跑 OCR / 重新向量化」
    產生多個並行任務互相衝突（重複 chunk / FAISS 孤兒）。
    """
    return (
        db.query(models.BackgroundTask)
        .filter(
            models.BackgroundTask.document_id == document_id,
            models.BackgroundTask.task_type.in_(_DOC_MUTATING_TASKS),
            models.BackgroundTask.status.in_(("pending", "running", "processing")),
        )
        .order_by(models.BackgroundTask.created_at.desc())
        .first()
    )


def _metadata_service(db: Session) -> MetadataService:
    return MetadataService(db)


def _list_active_classifications(db: Session) -> List[models.ClassificationCategory]:
    return (
        db.query(models.ClassificationCategory)
        .filter(models.ClassificationCategory.is_active.is_(True))
        .order_by(models.ClassificationCategory.name)
        .all()
    )


def _find_metadata_field(metadata_service: MetadataService, field_name: str, fields=None):
    candidates = fields if fields is not None else metadata_service.list_fields(active_only=True)
    for field in candidates:
        if field.name == field_name:
            return field
    return None


def _match_option_value(options, candidate: Optional[str]) -> Optional[str]:
    if not candidate:
        return None

    normalized = candidate.strip().lower()
    for option in options or []:
        value = getattr(option, "value", None)
        display = getattr(option, "display_value", None)
        if value and value.lower() == normalized:
            return value
        if display and display.lower() == normalized:
            return value
    return None


def _generate_classification_code(db: Session, name: str) -> str:
    base = re.sub(r"[^A-Za-z0-9]", "", name).upper() or "CLS"
    base = base[:12]
    candidate = base
    counter = 1
    while (
        db.query(models.ClassificationCategory)
        .filter(models.ClassificationCategory.code == candidate)
        .first()
        is not None
    ):
        counter += 1
        candidate = f"{base[:10]}{counter:02d}"
    return candidate


def _ensure_classification_category(
    db: Session,
    *,
    name: str,
    description: Optional[str],
) -> models.ClassificationCategory:
    normalized_name = name.strip()
    existing = (
        db.query(models.ClassificationCategory)
        .filter(func.lower(models.ClassificationCategory.name) == normalized_name.lower())
        .first()
    )
    if existing:
        return existing

    code = _generate_classification_code(db, normalized_name)
    category = models.ClassificationCategory(
        name=normalized_name,
        code=code,
        description=description or None,
        is_active=True,
    )
    db.add(category)
    db.commit()
    db.refresh(category)
    return category


def _ensure_project_option(
    metadata_service: MetadataService,
    *,
    display_name: str,
    description: Optional[str],
) -> models.MetadataOption:
    field = _find_metadata_field(metadata_service, "project_id")
    if field is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="專案欄位尚未在系統中設定",
        )

    normalized_display = display_name.strip()
    for option in field.options:
        if option.display_value == normalized_display or option.value == normalized_display:
            return option

    value_base = re.sub(r"[^A-Za-z0-9]", "_", normalized_display).strip("_").lower() or "proj"
    candidate = value_base[:32]
    existing_values = {opt.value for opt in field.options}
    counter = 1
    while candidate in existing_values:
        candidate = f"{value_base[:24]}_{counter}"
        counter += 1

    option = metadata_service.add_option(
        field,
        value=candidate,
        display_value=normalized_display,
        order_index=len(field.options),
    )
    if description:
        option.display_value = normalized_display
    return option


@router.get("", response_model=schemas.DocumentListResponse)
@router.get("/", response_model=schemas.DocumentListResponse)
def list_documents(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    search_term: Optional[str] = None,
    classification_id: Optional[str] = None,
    file_type: Optional[str] = None,
    project_id: Optional[str] = None,
    keywords: Optional[str] = None,
    folder_id: Optional[str] = None,
    folder_ids: Optional[str] = None,  # comma-separated folder IDs (recursive selection)
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    doc_service = _doc_service(db)

    metadata_filters: Dict[str, object] = {}
    if file_type:
        metadata_filters["file_type"] = file_type
    if project_id:
        metadata_filters["project_id"] = project_id
    if keywords:
        metadata_filters["keywords"] = [item.strip() for item in keywords.split(",") if item.strip()]

    parsed_folder_ids = (
        [fid.strip() for fid in folder_ids.split(",") if fid.strip()]
        if folder_ids else None
    )

    documents, total = doc_service.list(
        page=page,
        page_size=page_size,
        search_term=search_term,
        classification_id=classification_id,
        metadata_filters=metadata_filters or None,
        folder_id=folder_id,
        folder_ids=parsed_folder_ids,
    )
    return schemas.DocumentListResponse(items=documents, total=total, page=page, page_size=page_size)


@router.get("/classifications", response_model=List[schemas.ClassificationSummary])
def list_classifications(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _ = current_user
    categories = _list_active_classifications(db)
    return [schemas.ClassificationSummary.model_validate(cat) for cat in categories]


@router.get("/keywords", response_model=List[str])
def list_all_keywords(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """回傳系統中所有文件使用過的唯一關鍵字清單（排序後）。"""
    _ = current_user
    rows = db.query(models.Document.metadata_data).filter(
        models.Document.is_archived == False  # noqa: E712
    ).all()
    kw_set: set = set()
    for (meta,) in rows:
        if not isinstance(meta, dict):
            continue
        kws = meta.get("keywords")
        if isinstance(kws, list):
            for k in kws:
                if k and isinstance(k, str):
                    kw_set.add(k)
    return sorted(kw_set)


@router.post("/suggestions/accept", response_model=schemas.SuggestionAcceptanceResponse)
def accept_suggestion_recommendations(
    payload: schemas.SuggestionAcceptanceRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    metadata_service = _metadata_service(db)

    classification_obj = None
    if payload.classification and payload.classification.name:
        classification_obj = _ensure_classification_category(
            db,
            name=payload.classification.name,
            description=payload.classification.description,
        )

    project_option = None
    if payload.project and payload.project.display_name:
        project_option = _ensure_project_option(
            metadata_service,
            display_name=payload.project.display_name,
            description=payload.project.description,
        )

    return schemas.SuggestionAcceptanceResponse(
        classification=schemas.ClassificationSummary.model_validate(classification_obj)
        if classification_obj
        else None,
        project_option=schemas.MetadataOptionRead.model_validate(project_option) if project_option else None,
    )


@router.post("", response_model=schemas.DocumentRead, status_code=status.HTTP_201_CREATED)
@router.post("/", response_model=schemas.DocumentRead, status_code=status.HTTP_201_CREATED)
def create_document(
    payload: schemas.DocumentCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    logger.info(f"收到文件創建請求：標題='{payload.title}'，用戶={current_user.username}，is_image_based={payload.is_image_based}")

    doc_service = _doc_service(db)
    metadata_service = _metadata_service(db)
    required_fields = metadata_service.list_required_fields()
    doc_service.validate_metadata(payload.metadata, required_fields)

    # Validate and get classification category
    classification = ClassificationValidator.get_active_or_none(db, payload.classification_id)

    logger.info(f"開始創建文件：分類={classification.name if classification else 'None'}，metadata={payload.metadata}")

    # 有 PDF 且非圖片型（圖片型走 OCR 流程）→ 全部背景執行
    if payload.source_pdf_path and not payload.is_image_based:
        import json as _json

        # 只建立文件記錄，不做 PDF 處理
        document = doc_service.create(
            title=payload.title,
            content=payload.content,
            metadata=payload.metadata,
            creator=current_user,
            classification=classification,
            pdf_temp_path=None,   # 不在這裡處理 PDF
            segments=None,
            ai_summary=payload.ai_summary,
            is_image_based=False,
            original_filename=payload.original_filename,
            force_vision=False,
            force_ocr=False,
        )

        if payload.force_ocr:
            task_type = "ocr_vectorize"
            task_msg = "等待 PaddleOCR 開始..."
        elif payload.force_vision:
            task_type = "vl_vectorize"
            task_msg = "等待 VL 解析開始..."
        else:
            task_type = "vectorize"
            task_msg = "等待向量化開始..."

        bg_task = models.BackgroundTask(
            task_type=task_type,
            status="pending",
            progress=0,
            message=task_msg,
            document_id=document.id,
            creator_id=current_user.id,
        )
        db.add(bg_task)
        db.commit()
        db.refresh(bg_task)

        # force_ocr/force_vision 都不可重用前端傳來的 pdfminer segments
        skip_segments = payload.force_vision or payload.force_ocr
        background_tasks.add_task(
            run_document_finalize_task,
            task_id=bg_task.id,
            document_id=document.id,
            pdf_temp_path=payload.source_pdf_path,
            force_vision=payload.force_vision,
            segments_json=_json.dumps(payload.segments) if payload.segments and not skip_segments else None,
            original_filename=payload.original_filename,
            force_ocr=payload.force_ocr,
        )

        logger.info(f"背景任務已啟動：type={task_type}，task_id={bg_task.id}，document_id={document.id}")

        result = schemas.DocumentRead.model_validate(document)
        result.task_id = bg_task.id
        return result

    # 無 PDF 或圖片型 PDF（同步建立）
    document = doc_service.create(
        title=payload.title,
        content=payload.content,
        metadata=payload.metadata,
        creator=current_user,
        classification=classification,
        pdf_temp_path=payload.source_pdf_path,
        segments=payload.segments,
        ai_summary=payload.ai_summary,
        is_image_based=payload.is_image_based,
        original_filename=payload.original_filename,
        force_vision=False,
        force_ocr=False,
    )

    logger.info(f"文件創建成功：ID={document.id}，ocr_status={document.ocr_status}")

    return document


def _get_document_or_404(doc_service: DocumentService, document_id: str) -> models.Document:
    document = doc_service.get(document_id)
    if not document:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return document


@router.get("/search-text-all", response_model=schemas.CrossDocumentSearchResponse)
def search_all_documents_text(
    q: str = Query(..., min_length=1, description="搜尋關鍵字"),
    classification_id: Optional[str] = None,
    file_type: Optional[str] = None,
    project_id: Optional[str] = None,
    folder_id: Optional[str] = None,
    folder_ids: Optional[str] = None,  # comma-separated, for recursive folder filter
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    跨文件全文檢索

    支援：
    - 大小寫不敏感
    - 忽略多餘空格
    - 尊重當前篩選條件
    - 返回匹配的文件、頁碼和上下文
    """
    # 正規化搜尋關鍵字
    normalized_query = " ".join(q.strip().split())

    if not normalized_query:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="搜尋關鍵字不可為空"
        )

    # 構建文件查詢條件
    doc_query = db.query(models.Document)

    # 應用篩選條件
    if classification_id:
        doc_query = doc_query.filter(models.Document.classification_id == classification_id)

    if file_type:
        doc_query = doc_query.filter(
            func.json_extract(models.Document.metadata_data, '$.file_type') == file_type
        )

    if project_id:
        doc_query = doc_query.filter(
            func.json_extract(models.Document.metadata_data, '$.project_id') == project_id
        )

    # 資料夾範圍篩選（遞迴：folder_ids 優先）
    if folder_ids:
        ids = [fid.strip() for fid in folder_ids.split(",") if fid.strip()]
        doc_query = doc_query.filter(models.Document.folder_id.in_(ids))
    elif folder_id == "__root__":
        doc_query = doc_query.filter(models.Document.folder_id == None)  # noqa: E711
    elif folder_id:
        doc_query = doc_query.filter(models.Document.folder_id == folder_id)

    # 獲取符合篩選條件的文件 IDs
    document_ids = [doc.id for doc in doc_query.all()]

    if not document_ids:
        return schemas.CrossDocumentSearchResponse(
            query=normalized_query,
            total_matches=0,
            total_documents=0,
            matches=[]
        )

    # 在這些文件的 chunks 中搜尋
    chunks = (
        db.query(models.DocumentChunk, models.Document.title)
        .join(models.Document, models.DocumentChunk.document_id == models.Document.id)
        .filter(
            models.DocumentChunk.document_id.in_(document_ids),
            func.lower(models.DocumentChunk.text).contains(func.lower(normalized_query))
        )
        .order_by(models.Document.title, models.DocumentChunk.page, models.DocumentChunk.paragraph_index)
        .all()
    )

    # 構建搜尋結果
    matches = []
    unique_documents = set()

    for chunk, doc_title in chunks:
        unique_documents.add(chunk.document_id)

        # 找出匹配的文字片段
        text_lower = chunk.text.lower()
        query_lower = normalized_query.lower()
        start_idx = text_lower.find(query_lower)

        if start_idx != -1:
            matched_text = chunk.text[start_idx:start_idx + len(normalized_query)]

            # 生成摘錄
            context_before = 50
            context_after = 50
            snippet_start = max(0, start_idx - context_before)
            snippet_end = min(len(chunk.text), start_idx + len(normalized_query) + context_after)
            snippet = chunk.text[snippet_start:snippet_end]

            if snippet_start > 0:
                snippet = "..." + snippet
            if snippet_end < len(chunk.text):
                snippet = snippet + "..."
        else:
            matched_text = normalized_query
            snippet = chunk.text[:100]
            if len(chunk.text) > 100:
                snippet += "..."

        matches.append(
            schemas.CrossDocumentSearchMatch(
                document_id=chunk.document_id,
                document_title=doc_title,
                page=chunk.page or 0,
                paragraph_index=chunk.paragraph_index or 0,
                text=chunk.text,
                snippet=snippet,
                matched_text=matched_text,
            )
        )

    return schemas.CrossDocumentSearchResponse(
        query=normalized_query,
        total_matches=len(matches),
        total_documents=len(unique_documents),
        matches=matches,
    )


@router.get("/notes", response_model=List[schemas.GlobalNoteRead])
@router.get("/notes/", response_model=List[schemas.GlobalNoteRead])
def list_all_notes(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """列出當前使用者所有文件的筆記（跨文件）"""
    notes = (
        db.query(models.DocumentNote)
        .join(models.Document, models.DocumentNote.document_id == models.Document.id)
        .filter(models.DocumentNote.user_id == current_user.id)
        .order_by(models.DocumentNote.created_at.desc())
        .all()
    )
    return [
        schemas.GlobalNoteRead(
            id=n.id,
            document_id=n.document_id,
            document_title=n.document.title,
            question=n.question,
            answer=n.answer,
            created_at=n.created_at,
            updated_at=n.updated_at,
        )
        for n in notes
    ]


@router.get("/{document_id}", response_model=schemas.DocumentRead)
def get_document(
    document_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    doc_service = _doc_service(db)
    document = _get_document_or_404(doc_service, document_id)
    return document


@router.put("/{document_id}", response_model=schemas.DocumentRead)
def update_document(
    document_id: str,
    payload: schemas.DocumentUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    doc_service = _doc_service(db)
    metadata_service = _metadata_service(db)
    document = _get_document_or_404(doc_service, document_id)

    if payload.metadata is not None:
        doc_service.validate_metadata(payload.metadata, metadata_service.list_required_fields())

    # Validate and get classification category
    classification = ClassificationValidator.get_active_or_none(db, payload.classification_id)

    # Handle folder assignment
    if payload.folder_id is not None:
        if payload.folder_id == "__unset__":
            document.folder_id = None
        else:
            folder = db.query(models.Folder).filter(models.Folder.id == payload.folder_id).first()
            if not folder:
                raise HTTPException(status_code=404, detail="資料夾不存在")
            document.folder_id = folder.id

    updated = doc_service.update(
        document,
        title=payload.title,
        content=payload.content,
        metadata=payload.metadata,
        ai_summary=payload.ai_summary,
        classification=classification,
        pdf_temp_path=payload.source_pdf_path,
    )
    return updated


@router.put("/{document_id}/metadata", response_model=schemas.DocumentRead)
def update_document_metadata(
    document_id: str,
    payload: schemas.DocumentMetadataUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    doc_service = _doc_service(db)
    metadata_service = _metadata_service(db)
    document = _get_document_or_404(doc_service, document_id)

    metadata = dict(document.metadata_data or {})
    if payload.metadata:
        metadata.update(payload.metadata)

    keywords = metadata.get("keywords")
    if payload.add_keywords:
        if not isinstance(keywords, list):
            if keywords is None:
                keywords = []
            elif isinstance(keywords, str):
                keywords = [keywords]
            else:
                keywords = []
        for kw in payload.add_keywords:
            if kw not in keywords:
                keywords.append(kw)
        metadata["keywords"] = keywords

    if payload.remove_keywords and isinstance(keywords, list):
        metadata["keywords"] = [kw for kw in keywords if kw not in payload.remove_keywords]

    doc_service.validate_metadata(metadata, metadata_service.list_required_fields())
    updated = doc_service.update(document, metadata=metadata)
    return updated


@router.post("/{document_id}/classify", response_model=schemas.DocumentRead)
def classify_document(
    document_id: str,
    payload: schemas.DocumentClassificationApply,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    doc_service = _doc_service(db)
    document = _get_document_or_404(doc_service, document_id)

    # Validate and get classification category
    classification = ClassificationValidator.get_active_or_404(db, payload.classification_id)

    updated = doc_service.apply_classification(document, classification=classification)
    return updated


@router.post("/upload/", response_model=schemas.DocumentUploadResponse)
async def upload_pdf_for_extraction(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    if file.content_type not in {"application/pdf", "application/octet-stream"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only PDF files are supported")

    pdf_bytes = await file.read()
    FileValidator.validate_pdf_not_empty(pdf_bytes)

    temp_dir = Path(settings.PDF_TEMP_DIR)
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_filename = f"{uuid.uuid4()}.pdf"
    temp_path = temp_dir / temp_filename
    temp_path.write_bytes(pdf_bytes)

    try:
        try:
            text, segments = extract_text_and_segments(pdf_bytes)
        except PDFPasswordIncorrect:
            temp_path.unlink(missing_ok=True)
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="PDF 檔案已加密，請提供未加密版本")

        # 檢測圖片型 PDF（掃描件、傳真件）
        text_length = len(text.strip()) if text else 0
        is_image_based = text_length < 100  # 文字量少於 100 字視為圖片型 PDF

        if is_image_based:
            # 圖片型 PDF：獲取頁數並返回特殊響應
            try:
                total_pages = get_pdf_page_count(str(temp_path))
            except Exception:
                total_pages = None

            logger.info(f"Detected image-based PDF: {file.filename}, pages: {total_pages}, text_length: {text_length}")

            return schemas.DocumentUploadResponse(
                filename=file.filename or "uploaded.pdf",
                text=text or "",  # 可能有少量文字
                segments=[],  # 圖片型 PDF 沒有有效的文字段落
                suggested_metadata={},
                suggestion=schemas.AISuggestion(),  # 空的建議
                pdf_temp_path=str(temp_path),
                is_image_based=True,
                total_pages=total_pages,
            )

        if len(text) > 20000:
            text = text[:20000]

        metadata_service = _metadata_service(db)
        metadata_fields = metadata_service.list_fields(active_only=True)

        classifications = _list_active_classifications(db)
        classification_prompt = []
        for category in classifications:
            label = category.name
            if category.code:
                label = f"{category.name} ({category.code})"
            classification_prompt.append(label)

        project_field = _find_metadata_field(metadata_service, "project_id", metadata_fields)
        project_prompt = []
        project_options = getattr(project_field, "options", []) if project_field else []
        for option in project_options:
            label = option.display_value or option.value
            if option.display_value and option.value and option.display_value.lower() != option.value.lower():
                label = f"{option.display_value} ({option.value})"
            project_prompt.append(label)

        try:
            suggestion_payload = ai.generate_document_suggestion(
                text=text,
                classifications=classification_prompt,
                projects=project_prompt,
                segments=[segment.model_dump() for segment in segments],
            )
        except Exception as exc:  # pragma: no cover - external API failure handling
            logger.warning("Gemini suggestion request failed: %s", exc)
            suggestion_payload = {}

        suggestion = schemas.AISuggestion(**(suggestion_payload or {}))

        keyword_candidates: List[str] = []
        if suggestion.keywords:
            keyword_candidates.extend(suggestion.keywords)
        keyword_candidates.extend(suggest_keywords(text))

        merged_keywords: List[str] = []
        seen_keywords = set()
        for keyword in keyword_candidates:
            if not keyword:
                continue
            normalized = keyword.lower()
            if normalized in seen_keywords:
                continue
            seen_keywords.add(normalized)
            merged_keywords.append(keyword)

        suggested_metadata: Dict[str, object] = {"keywords": merged_keywords}

        file_type_field = _find_metadata_field(metadata_service, "file_type", metadata_fields)
        if file_type_field:
            matched_file_type = _match_option_value(getattr(file_type_field, "options", []), suggestion.metadata.get("file_type"))
            if matched_file_type:
                suggested_metadata["file_type"] = matched_file_type

        if project_field:
            matched_project = (
                _match_option_value(project_options, suggestion.metadata.get("project_id"))
                or _match_option_value(project_options, suggestion.project)
            )
            if matched_project:
                suggested_metadata["project_id"] = matched_project

        return schemas.DocumentUploadResponse(
            filename=file.filename or "uploaded.pdf",
            text=text,
            segments=segments,
            suggested_metadata=suggested_metadata,
            suggestion=suggestion,
            pdf_temp_path=str(temp_path),
        )
    except Exception:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
        raise


@router.post("/upload/async", response_model=schemas.TaskRead, status_code=status.HTTP_202_ACCEPTED)
async def upload_pdf_async(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """非同步上傳 PDF：立刻返回 task_id，文字提取與 AI 分析在背景執行"""
    # Audit M：上傳觸發 OCR/AI 分析等重運算 → 每人配額
    from ...utils.ratelimit import check_quota
    check_quota(current_user.id, "upload_async", limit=60, per_seconds=3600)

    if file.content_type not in {"application/pdf", "application/octet-stream"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only PDF files are supported")

    pdf_bytes = await file.read()
    FileValidator.validate_pdf_not_empty(pdf_bytes)

    temp_dir = Path(settings.PDF_TEMP_DIR)
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_filename = f"{uuid.uuid4()}.pdf"
    temp_path = temp_dir / temp_filename
    temp_path.write_bytes(pdf_bytes)

    bg_task = models.BackgroundTask(
        task_type="pdf_analyze",
        status="pending",
        progress=0,
        message="等待分析開始...",
        creator_id=current_user.id,
    )
    db.add(bg_task)
    db.commit()
    db.refresh(bg_task)

    background_tasks.add_task(
        run_upload_analysis_task,
        task_id=bg_task.id,
        file_path=str(temp_path),
        filename=file.filename or "uploaded.pdf",
    )

    return bg_task


@router.get("/{document_id}/pdf")
def download_document_pdf(
    document_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    doc_service = _doc_service(db)
    document = _get_document_or_404(doc_service, document_id)

    if not document.pdf_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PDF 尚未上傳或已被移除")

    # Security: Validate file path to prevent path traversal attacks
    # Ensure the file is within the storage directory
    validated_path = validate_file_path(
        file_path=document.pdf_path,
        base_dir=settings.FILE_STORAGE_DIR
    )

    # Use document title for the download filename
    # Sanitize title to ensure it's a valid filename
    safe_title = "".join(c for c in document.title if c.isalnum() or c in (' ', '.', '_', '-')).strip()
    if not safe_title:
        safe_title = f"document_{document_id}"
    
    download_filename = f"{safe_title}.pdf"

    return FileResponse(validated_path, media_type="application/pdf", filename=download_filename)


@router.get("/{document_id}/analysis")
def get_document_analysis(
    document_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """取得當前使用者對此文件的 PDF 分析對話紀錄"""
    _get_document_or_404(_doc_service(db), document_id)
    row = db.query(models.DocumentUserAnalysis).filter(
        models.DocumentUserAnalysis.document_id == document_id,
        models.DocumentUserAnalysis.user_id == current_user.id,
    ).first()
    return {"messages": row.messages if row else []}


@router.delete("/{document_id}/analysis", status_code=status.HTTP_204_NO_CONTENT)
def clear_document_analysis(
    document_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """清除當前使用者對此文件的 PDF 分析對話紀錄"""
    db.query(models.DocumentUserAnalysis).filter(
        models.DocumentUserAnalysis.document_id == document_id,
        models.DocumentUserAnalysis.user_id == current_user.id,
    ).delete()
    db.commit()
    return None


@router.delete("/{document_id}/history", status_code=status.HTTP_204_NO_CONTENT)
def clear_document_history(
    document_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """[deprecated] 清除文件的 AI 對話歷史 — 改用 DELETE /{document_id}/analysis"""
    db.query(models.DocumentUserAnalysis).filter(
        models.DocumentUserAnalysis.document_id == document_id,
        models.DocumentUserAnalysis.user_id == current_user.id,
    ).delete()
    db.commit()
    return None





# ===== Document Notes =====

@router.post("/{document_id}/notes", response_model=schemas.DocumentNoteRead)
def create_document_note(
    document_id: str,
    payload: schemas.DocumentNoteCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """新增文件筆記"""
    doc_service = _doc_service(db)
    document = _get_document_or_404(doc_service, document_id)
    
    note = models.DocumentNote(
        document_id=document.id,
        user_id=current_user.id,
        question=payload.question,
        answer=payload.answer,
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    return note


@router.get("/{document_id}/notes", response_model=List[schemas.DocumentNoteRead])
def list_document_notes(
    document_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """列出文件筆記"""
    doc_service = _doc_service(db)
    _get_document_or_404(doc_service, document_id)
    
    notes = db.query(models.DocumentNote).filter(
        models.DocumentNote.document_id == document_id,
        models.DocumentNote.user_id == current_user.id,
    ).order_by(models.DocumentNote.created_at.desc()).all()
    return notes


@router.put("/{document_id}/notes/{note_id}", response_model=schemas.DocumentNoteRead)
def update_document_note(
    document_id: str,
    note_id: str,
    payload: schemas.DocumentNoteUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """更新文件筆記"""
    note = db.query(models.DocumentNote).filter(
        models.DocumentNote.id == note_id,
        models.DocumentNote.document_id == document_id,
        models.DocumentNote.user_id == current_user.id,
    ).first()

    if not note:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="筆記不存在")

    if payload.question is not None:
        note.question = payload.question
    if payload.answer is not None:
        note.answer = payload.answer
        
    db.commit()
    db.refresh(note)
    return note


@router.delete("/{document_id}/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document_note(
    document_id: str,
    note_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """刪除文件筆記"""
    note = db.query(models.DocumentNote).filter(
        models.DocumentNote.id == note_id,
        models.DocumentNote.document_id == document_id,
        models.DocumentNote.user_id == current_user.id,
    ).first()

    if not note:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="筆記不存在")

    db.delete(note)
    db.commit()
    return None


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(
    document_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    """刪除文件及其相關資料（chunks、向量、PDF 檔案）"""
    doc_service = _doc_service(db)
    document = _get_document_or_404(doc_service, document_id)

    # 執行刪除
    doc_service.delete(document)
    return None


@router.post("/{document_id}/re-vectorize", response_model=schemas.DocumentRead)
def re_vectorize_document(
    document_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    """
    重新向量化文件

    只重新生成向量嵌入，不重新提取 PDF 文字內容
    適用於：清空向量值後重建、切換 embedding 模型
    """
    doc_service = _doc_service(db)
    document = _get_document_or_404(doc_service, document_id)

    # Audit：同文件去重 —— 若已有進行中的重跑 OCR / 向量化任務,拒絕以免並行衝突。
    inflight = _inflight_doc_task(db, document.id)
    if inflight is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="此文件正在處理中（OCR / 向量化），請待完成後再操作。",
        )

    try:
        # 只重新生成 embeddings，不重新提取 PDF 文字
        doc_service._re_embed_existing_chunks(document)
        db.refresh(document, attribute_names=["classification", "chunks"])
        logger.info(f"文件 {document_id} 已重新向量化（共 {len(document.chunks)} 個向量塊）")

        return schemas.DocumentRead(
            id=document.id,
            title=document.title,
            content=document.content,
            metadata_data=document.metadata_data or {},
            classification=schemas.ClassificationSummary(
                id=document.classification.id,
                name=document.classification.name,
                code=document.classification.code,
            ) if document.classification else None,
            creator_id=document.creator_id,
            created_at=document.created_at,
            updated_at=document.updated_at,
            is_archived=document.is_archived,
            pdf_path=document.pdf_path,
            ai_summary=document.ai_summary,
        )
    except ValueError as exc:
        # 沒有現有的 chunks
        logger.error(f"重新向量化失敗：{exc}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc)
        )
    except Exception as exc:
        logger.error(f"重新向量化失敗：{exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"重新向量化失敗：{str(exc)}"
        )


@router.post("/{document_id}/reocr", response_model=schemas.TaskRead, status_code=status.HTTP_202_ACCEPTED)
def reocr_document(
    document_id: str,
    background_tasks: BackgroundTasks,
    payload: Optional[dict] = None,
    db: Session = Depends(get_db),
    current_admin=Depends(get_current_admin_user),
):
    """高精度重跑單份文件 OCR（預設 engine=rapid, tier=server）→ 重建 chunks + 重新向量化。

    背景執行（server 等級在 CPU 上很慢，GPU 正式機才快）。前端 poll task 進度。
    """
    _ = current_admin
    payload = payload or {}
    engine = payload.get("engine") or "rapid"
    tier = payload.get("tier") or "server"
    if engine not in {"rapid", "pp_structure"}:
        raise HTTPException(status_code=400, detail="engine must be rapid|pp_structure")
    if tier not in {"mobile", "server"}:
        raise HTTPException(status_code=400, detail="tier must be mobile|server")

    doc_service = _doc_service(db)
    document = _get_document_or_404(doc_service, document_id)
    if not document.pdf_path or not Path(document.pdf_path).exists():
        raise HTTPException(status_code=400, detail="此文件沒有可重跑的原始 PDF")

    # Audit：同文件去重 —— 避免使用者重複點擊產生多個並行重跑（會互相打架、造成
    # 重複 chunk / FAISS 孤兒）。已有進行中的重跑/向量化任務就回傳既有任務,不新建。
    existing = _inflight_doc_task(db, document.id)
    if existing is not None:
        return schemas.TaskRead.model_validate(existing)

    bg_task = models.BackgroundTask(
        task_type="reocr",
        status="pending",
        progress=0,
        message=f"等待高精度 OCR（{engine}/{tier}）開始...",
        document_id=document.id,
        creator_id=current_admin.id,
    )
    db.add(bg_task)
    db.commit()
    db.refresh(bg_task)

    background_tasks.add_task(
        run_document_reocr_task,
        task_id=bg_task.id,
        document_id=document.id,
        ocr_engine=engine,
        ocr_tier=tier,
    )
    return bg_task


@router.get("/{document_id}/search-text", response_model=schemas.TextSearchResponse)
def search_document_text(
    document_id: str,
    q: str = Query(..., min_length=1, description="搜尋關鍵字"),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    在文件中搜尋文字（全文檢索）

    支援：
    - 大小寫不敏感
    - 忽略多餘空格
    - 返回匹配的頁碼和上下文
    """
    doc_service = _doc_service(db)
    document = _get_document_or_404(doc_service, document_id)

    # 正規化搜尋關鍵字（移除多餘空格）
    normalized_query = " ".join(q.strip().split())

    if not normalized_query:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="搜尋關鍵字不可為空"
        )

    # 查詢 DocumentChunk，使用大小寫不敏感搜尋
    chunks = (
        db.query(models.DocumentChunk)
        .filter(
            models.DocumentChunk.document_id == document_id,
            func.lower(models.DocumentChunk.text).contains(func.lower(normalized_query))
        )
        .order_by(models.DocumentChunk.page, models.DocumentChunk.paragraph_index)
        .all()
    )

    # 構建搜尋結果
    matches = []
    for chunk in chunks:
        # 找出匹配的文字片段（用於高亮）
        text_lower = chunk.text.lower()
        query_lower = normalized_query.lower()
        start_idx = text_lower.find(query_lower)

        if start_idx != -1:
            # 提取匹配的原始文字（保留大小寫）
            matched_text = chunk.text[start_idx:start_idx + len(normalized_query)]

            # 生成摘錄：關鍵字前後各 50 字
            context_before = 50
            context_after = 50

            snippet_start = max(0, start_idx - context_before)
            snippet_end = min(len(chunk.text), start_idx + len(normalized_query) + context_after)

            snippet = chunk.text[snippet_start:snippet_end]

            # 添加省略號
            if snippet_start > 0:
                snippet = "..." + snippet
            if snippet_end < len(chunk.text):
                snippet = snippet + "..."
        else:
            # 如果找不到精確匹配（可能因為空格），就用整段文字
            matched_text = normalized_query
            # 截取前 100 字作為摘錄
            snippet = chunk.text[:100]
            if len(chunk.text) > 100:
                snippet += "..."

        matches.append(
            schemas.TextSearchMatch(
                page=chunk.page or 0,
                paragraph_index=chunk.paragraph_index or 0,
                text=chunk.text,
                snippet=snippet,
                matched_text=matched_text,
            )
        )

    return schemas.TextSearchResponse(
        query=normalized_query,
        total_matches=len(matches),
        matches=matches,
    )


@router.get("/{document_id}/page/{page_number}/text")
def get_page_text(
    document_id: str,
    page_number: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    獲取指定頁面的文字內容

    從 document_chunks 合併查詢（僅支持普通 PDF）
    圖片型 PDF 不提供文字內容
    """
    doc_service = _doc_service(db)
    document = _get_document_or_404(doc_service, document_id)

    # 檢查是否為圖片型 PDF
    if document.is_image_based:
        return {
            "page": page_number,
            "text": "",
            "source": "image_based",  # 標記為圖片型 PDF
            "message": "圖片型 PDF 不提供文字內容，僅支持預覽"
        }

    # 查詢 chunks（普通 PDF）
    chunks = (
        db.query(models.DocumentChunk)
        .filter(
            models.DocumentChunk.document_id == document_id,
            models.DocumentChunk.page == page_number
        )
        .order_by(models.DocumentChunk.paragraph_index)
        .all()
    )

    if not chunks:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"第 {page_number} 頁沒有文字內容"
        )

    # 合併所有 chunks 的文字
    full_text = "\n\n".join(chunk.text for chunk in chunks if chunk.text)

    return {
        "page": page_number,
        "text": full_text,
        "source": "chunks"  # 標記文字來源
    }


@router.post("/{document_id}/ocr/process")
def process_document_ocr(
    document_id: str,
    request: schemas.OCRProcessRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    圖片型 PDF 處理 - 僅支持預覽模式

    此功能已簡化：圖片型 PDF 僅提供預覽功能，不執行 OCR 識別
    請確保填寫必要的 metadata 後保存文件
    """
    logger.info(f"收到圖片型 PDF 處理請求：文件 ID={document_id}，用戶={current_user.username}")

    doc_service = _doc_service(db)
    document = _get_document_or_404(doc_service, document_id)

    # 檢查是否為圖片型 PDF
    if not document.is_image_based:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="此文件不是圖片型 PDF"
        )

    # 標記為 skipped（僅供預覽）
    document.ocr_status = "skipped"
    db.commit()

    return schemas.OCRStatusResponse(
        document_id=document.id,
        ocr_status="skipped",
        is_image_based=True,
        message="圖片型 PDF 僅支持預覽功能，請確保已填寫必要的 metadata 後保存"
    )


@router.get("/{document_id}/ocr/status", response_model=schemas.OCRStatusResponse)
def get_ocr_status(
    document_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    查詢文件的 OCR 處理狀態
    """
    doc_service = _doc_service(db)
    document = _get_document_or_404(doc_service, document_id)

    # 計算進度（如果正在處理中）
    progress = None
    if document.ocr_status == "processing":
        # 目前是同步處理，無法精確追蹤進度
        # 未來可透過背景任務系統提供精確進度
        progress = 50
    elif document.ocr_status == "completed":
        progress = 100
    elif document.ocr_status in ["pending", "not_needed", "skipped", "failed"]:
        progress = 0

    message = None
    if document.ocr_status == "completed":
        # 統計處理結果
        chunk_count = db.query(func.count(models.DocumentChunk.id)).filter(
            models.DocumentChunk.document_id == document_id
        ).scalar()
        message = f"OCR 完成，已建立 {chunk_count} 個文字段落"
    elif document.ocr_status == "failed":
        message = "OCR 處理失敗，請重試"
    elif document.ocr_status == "skipped":
        message = "已跳過 OCR 處理"
    elif document.ocr_status == "processing":
        message = "正在進行 OCR 識別..."
    elif document.ocr_status == "pending":
        message = "等待處理中..."

    return schemas.OCRStatusResponse(
        document_id=document.id,
        ocr_status=document.ocr_status,
        is_image_based=document.is_image_based,
        ocr_method=document.ocr_method,
        progress=progress,
        message=message
    )


# ── 向量塊管理 ────────────────────────────────────────────────────────────────

def _get_chunk_or_404(db: Session, document_id: str, chunk_id: str) -> models.DocumentChunk:
    chunk = (
        db.query(models.DocumentChunk)
        .filter(
            models.DocumentChunk.id == chunk_id,
            models.DocumentChunk.document_id == document_id,
        )
        .first()
    )
    if not chunk:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="向量塊不存在")
    return chunk


def _chunk_to_read(chunk: models.DocumentChunk) -> schemas.ChunkRead:
    return schemas.ChunkRead(
        id=chunk.id,
        chunk_index=chunk.chunk_index,
        page=chunk.page,
        paragraph_index=chunk.paragraph_index,
        text=chunk.text,
        char_count=len(chunk.text),
        faiss_id=chunk.faiss_id,
    )


@router.get("/{document_id}/chunks", response_model=schemas.ChunkListResponse)
def list_document_chunks(
    document_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    doc_service = _doc_service(db)
    _get_document_or_404(doc_service, document_id)

    chunks = (
        db.query(models.DocumentChunk)
        .filter(models.DocumentChunk.document_id == document_id)
        .order_by(models.DocumentChunk.chunk_index)
        .all()
    )
    items = [_chunk_to_read(c) for c in chunks]
    total_chars = sum(i.char_count for i in items)
    return schemas.ChunkListResponse(
        items=items,
        total=len(items),
        total_chars=total_chars,
        avg_chars=total_chars // len(items) if items else 0,
    )


@router.post("/{document_id}/chunks", response_model=schemas.ChunkRead, status_code=status.HTTP_201_CREATED)
def create_document_chunk(
    document_id: str,
    payload: schemas.ChunkCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_admin_user),  # audit：chunk 寫入=改寫檢索語料，須 admin
):
    from ...services import vector_store
    doc_service = _doc_service(db)
    _get_document_or_404(doc_service, document_id)

    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="文字不可為空")

    max_index = (
        db.query(func.max(models.DocumentChunk.chunk_index))
        .filter(models.DocumentChunk.document_id == document_id)
        .scalar()
    ) or 0

    embeddings = ai.embed_texts([text])
    if not embeddings:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="向量化失敗")

    import uuid as _uuid
    faiss_id = int(_uuid.uuid4().int % (1 << 63))
    chunk = models.DocumentChunk(
        document_id=document_id,
        chunk_index=max_index + 1,
        page=payload.page,
        paragraph_index=None,
        text=text,
        embedding=embeddings[0],
        faiss_id=faiss_id,
    )
    db.add(chunk)
    db.commit()
    db.refresh(chunk)
    vector_store.add_embeddings({faiss_id: embeddings[0]})
    return _chunk_to_read(chunk)


@router.put("/{document_id}/chunks/{chunk_id}", response_model=schemas.ChunkRead)
def update_document_chunk(
    document_id: str,
    chunk_id: str,
    payload: schemas.ChunkUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_admin_user),  # audit：chunk 寫入=改寫檢索語料，須 admin
):
    from ...services import vector_store
    chunk = _get_chunk_or_404(db, document_id, chunk_id)

    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="文字不可為空")

    # 重新向量化
    embeddings = ai.embed_texts([text])
    if not embeddings:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="向量化失敗")

    # 更新 FAISS：先移除舊向量，再加入新向量
    vector_store.remove_embeddings([chunk.faiss_id])
    vector_store.add_embeddings({chunk.faiss_id: embeddings[0]})

    chunk.text = text
    chunk.embedding = embeddings[0]
    db.commit()
    db.refresh(chunk)
    return _chunk_to_read(chunk)


@router.delete("/{document_id}/chunks/{chunk_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document_chunk(
    document_id: str,
    chunk_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_admin_user),  # audit：chunk 寫入=改寫檢索語料，須 admin
):
    from ...services import vector_store
    chunk = _get_chunk_or_404(db, document_id, chunk_id)
    vector_store.remove_embeddings([chunk.faiss_id])
    db.delete(chunk)
    db.commit()
    return None


@router.post("/{document_id}/chunks/merge", response_model=schemas.ChunkRead, status_code=status.HTTP_201_CREATED)
def merge_document_chunks(
    document_id: str,
    payload: schemas.ChunkMergeRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_admin_user),  # audit：chunk 寫入=改寫檢索語料，須 admin
):
    """合併多個 chunk 成一個，依照 chunk_index 順序合併文字。"""
    from ...services import vector_store
    if len(payload.chunk_ids) < 2:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="至少需要選取 2 個向量塊才能合併")

    chunks = (
        db.query(models.DocumentChunk)
        .filter(
            models.DocumentChunk.document_id == document_id,
            models.DocumentChunk.id.in_(payload.chunk_ids),
        )
        .order_by(models.DocumentChunk.chunk_index)
        .all()
    )
    if len(chunks) != len(payload.chunk_ids):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="部分向量塊不存在")

    merged_text = "\n\n".join(c.text for c in chunks)
    first_page = chunks[0].page
    min_index = min(c.chunk_index for c in chunks)

    # 向量化合併後的文字
    embeddings = ai.embed_texts([merged_text])
    if not embeddings:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="向量化失敗")

    # 刪除舊塊
    old_faiss_ids = [c.faiss_id for c in chunks]
    vector_store.remove_embeddings(old_faiss_ids)
    for c in chunks:
        db.delete(c)
    db.flush()

    # 建立新合併塊
    import uuid as _uuid
    faiss_id = int(_uuid.uuid4().int % (1 << 63))
    new_chunk = models.DocumentChunk(
        document_id=document_id,
        chunk_index=min_index,
        page=first_page,
        paragraph_index=None,
        text=merged_text,
        embedding=embeddings[0],
        faiss_id=faiss_id,
    )
    db.add(new_chunk)
    db.commit()
    db.refresh(new_chunk)
    vector_store.add_embeddings({faiss_id: embeddings[0]})
    return _chunk_to_read(new_chunk)


@router.post("/{document_id}/chunks/{chunk_id}/split", response_model=List[schemas.ChunkRead], status_code=status.HTTP_201_CREATED)
def split_document_chunk(
    document_id: str,
    chunk_id: str,
    payload: schemas.ChunkSplitRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_admin_user),  # audit：chunk 寫入=改寫檢索語料，須 admin
):
    """在指定字元位置將一個 chunk 拆成兩個。"""
    from ...services import vector_store
    chunk = _get_chunk_or_404(db, document_id, chunk_id)

    if payload.split_at <= 0 or payload.split_at >= len(chunk.text):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"拆分位置必須在 1 到 {len(chunk.text) - 1} 之間",
        )

    text_a = chunk.text[:payload.split_at].strip()
    text_b = chunk.text[payload.split_at:].strip()
    if not text_a or not text_b:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="拆分後兩塊均不可為空")

    embeddings = ai.embed_texts([text_a, text_b])
    if len(embeddings) != 2:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="向量化失敗")

    # 刪除原塊
    vector_store.remove_embeddings([chunk.faiss_id])
    original_index = chunk.chunk_index
    original_page = chunk.page
    db.delete(chunk)
    db.flush()

    import uuid as _uuid
    result_chunks = []
    for i, (text, emb) in enumerate(zip([text_a, text_b], embeddings)):
        faiss_id = int(_uuid.uuid4().int % (1 << 63))
        new_chunk = models.DocumentChunk(
            document_id=document_id,
            chunk_index=original_index + i,
            page=original_page,
            paragraph_index=None,
            text=text,
            embedding=emb,
            faiss_id=faiss_id,
        )
        db.add(new_chunk)
        result_chunks.append((faiss_id, emb, new_chunk))

    db.commit()
    faiss_map = {fid: emb for fid, emb, _ in result_chunks}
    vector_store.add_embeddings(faiss_map)

    return [_chunk_to_read(c) for _, _, c in result_chunks]
