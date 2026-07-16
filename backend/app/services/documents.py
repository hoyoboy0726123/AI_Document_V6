import logging
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, status
from sqlalchemy.orm import Session, selectinload

from .. import models
from ..core.config import settings
from . import ai, vector_store
from .ocr_pipeline import extract_image_pdf_blocks
from .pdf_processing import extract_text_and_segments, split_segments_into_chunks
from .system_config import SystemConfigService

logger = logging.getLogger(__name__)


def _generate_faiss_id() -> int:
    return int(uuid.uuid4().int % (1 << 63))


class DocumentService:
    def __init__(self, db: Session):
        self.db = db
        self._pdf_storage = Path(settings.PDF_STORAGE_DIR)
        self._pdf_temp = Path(settings.PDF_TEMP_DIR)
        self._pdf_storage.mkdir(parents=True, exist_ok=True)
        self._pdf_temp.mkdir(parents=True, exist_ok=True)

    # ---- Retrieval helpers ----
    def get(self, document_id: str) -> Optional[models.Document]:
        return (
            self.db.query(models.Document)
            .options(selectinload(models.Document.classification))
            .filter(models.Document.id == document_id)
            .first()
        )

    def list(
        self,
        *,
        page: int,
        page_size: int,
        search_term: Optional[str] = None,
        classification_id: Optional[str] = None,
        metadata_filters: Optional[Dict[str, Any]] = None,
        folder_id: Optional[str] = None,
        folder_ids: Optional[List[str]] = None,
    ) -> Tuple[List[models.Document], int]:
        query = (
            self.db.query(models.Document)
            .options(
                selectinload(models.Document.classification),
            )
        )

        if search_term:
            like_pattern = f"%{search_term}%"
            query = query.filter(models.Document.title.ilike(like_pattern))

        if classification_id:
            query = query.filter(models.Document.classification_id == classification_id)

        # folder_ids takes precedence (recursive: selected folder + all descendants)
        if folder_ids is not None:
            query = query.filter(models.Document.folder_id.in_(folder_ids))
        elif folder_id is not None:
            if folder_id == "__root__":
                query = query.filter(models.Document.folder_id == None)  # noqa: E711
            else:
                query = query.filter(models.Document.folder_id == folder_id)

        documents = query.order_by(models.Document.created_at.desc()).all()

        if metadata_filters:
            documents = [
                doc
                for doc in documents
                if self._metadata_match(doc.metadata_data or {}, metadata_filters)
            ]

        total = len(documents)
        start = max(page - 1, 0) * page_size
        end = start + page_size
        return documents[start:end], total

    # ---- ????? ----
    def create(
        self,
        *,
        title: str,
        content: Optional[str],
        metadata: Dict[str, Any],
        creator: models.User,
        classification: Optional[models.ClassificationCategory] = None,
        pdf_temp_path: Optional[str] = None,
        segments: Optional[List[Dict[str, Any]]] = None,
        ai_summary: Optional[str] = None,
        is_image_based: bool = False,
        original_filename: Optional[str] = None,
        force_vision: bool = False,
        force_ocr: bool = False,
    ) -> models.Document:
        document = models.Document(
            title=title,
            content=content,
            creator_id=creator.id,
            created_at=datetime.utcnow(),
            ai_summary=ai_summary,
            is_image_based=is_image_based,
            ocr_status="pending" if (is_image_based or force_ocr) else "not_needed",
        )
        document.metadata_data = metadata or {}
        if classification is not None:
            document.classification_id = classification.id
        self.db.add(document)
        self.db.commit()
        self.db.refresh(document, attribute_names=["classification"])

        if pdf_temp_path:
            self._finalize_pdf_and_index(
                document,
                pdf_temp_path,
                segments=segments,
                original_filename=original_filename,
                force_vision=force_vision,
                force_ocr=force_ocr,
            )

        return document

    def update(
        self,
        document: models.Document,
        *,
        title: Optional[str] = None,
        content: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        ai_summary: Optional[str] = None,
        classification: Optional[models.ClassificationCategory] = None,
        pdf_temp_path: Optional[str] = None,
    ) -> models.Document:
        if title is not None:
            document.title = title
        if content is not None:
            document.content = content
        if metadata is not None:
            document.metadata_data = metadata or {}
        if ai_summary is not None:
            document.ai_summary = ai_summary
        if classification is not None:
            document.classification_id = classification.id
        self.db.commit()
        self.db.refresh(document, attribute_names=["classification"])

        if pdf_temp_path:
            self._finalize_pdf_and_index(document, pdf_temp_path)

        return document

    def validate_metadata(self, metadata: Dict[str, Any], required_fields: List[models.MetadataField]) -> None:
        errors: List[str] = []
        metadata = metadata or {}

        for field in required_fields:
            value = metadata.get(field.name)

            if field.is_required and (value is None or (isinstance(value, str) and value.strip() == "")):
                errors.append(f"Missing required metadata: {field.display_name}")
                continue

            if value is None:
                continue

            try:
                if field.field_type in {"text", "textarea"}:
                    if not isinstance(value, str):
                        raise ValueError("must be a string")
                elif field.field_type == "number":
                    if not isinstance(value, (int, float)):
                        raise ValueError("must be a number")
                elif field.field_type == "date":
                    if isinstance(value, str):
                        datetime.fromisoformat(value)
                    else:
                        raise ValueError("must be an ISO date string")
                elif field.field_type == "select":
                    valid_values = {opt.value for opt in field.options if opt.is_active}
                    if value not in valid_values:
                        raise ValueError(f"value must be one of: {', '.join(valid_values)}")
                elif field.field_type == "multi_select":
                    if not isinstance(value, list):
                        raise ValueError("must be a list")
                    valid_values = {opt.value for opt in field.options if opt.is_active}
                    invalid = [item for item in value if item not in valid_values]
                    if invalid:
                        raise ValueError(f"invalid selection(s): {', '.join(invalid)}")
            except ValueError as exc:
                errors.append(f"{field.display_name} {exc}")

        if errors:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=errors)

    def apply_classification(
        self,
        document: models.Document,
        *,
        classification: models.ClassificationCategory,
    ) -> models.Document:
        document.classification_id = classification.id
        self.db.commit()
        self.db.refresh(document)
        return document

    def update_metadata_fields(self, document: models.Document, *, changes: Dict[str, Any]) -> models.Document:
        document.metadata_data = {**(document.metadata_data or {}), **(changes or {})}
        self.db.commit()
        self.db.refresh(document, attribute_names=["classification"])
        return document

    def delete(self, document: models.Document) -> None:
        """刪除文件及其相關資料（chunks、向量、KG、PDF 檔案）

        順序修正（audit C3/H5）：先把 DB 端（KG 邊 / user analysis / chunks / document）
        全部 commit 成功，再刪 FAISS 向量，最後才刪 PDF 檔。
        這樣即使 FAISS 刪除或檔案刪除失敗，也不會出現「DB 有 chunk 但向量已消失」
        或「刪一半 rollback」的單邊不一致；FAISS 殘留向量會在檢索時被 chunk_map 過濾掉。
        """
        document_id = document.id
        existing_chunks = (
            self.db.query(models.DocumentChunk)
            .filter(models.DocumentChunk.document_id == document_id)
            .all()
        )
        existing_faiss_ids = [chunk.faiss_id for chunk in existing_chunks if chunk.faiss_id]
        pdf_path = document.pdf_path

        # 1. 先清 KG（關係邊 + 本文件專屬的 document/section 結構節點），避免懸空邊與幽靈節點
        try:
            from . import kg_service
            kg_service.delete_kg_for_document(self.db, document_id)
        except Exception as e:  # KG 清理失敗不應阻擋刪除，但要留痕
            logger.warning(f"KG cleanup failed for document {document_id}: {e}")

        # 2. 清該文件的 per-user 分析狀態（避免懸空 document_id）
        try:
            self.db.query(models.DocumentUserAnalysis).filter(
                models.DocumentUserAnalysis.document_id == document_id
            ).delete(synchronize_session=False)
        except Exception as e:
            logger.warning(f"User-analysis cleanup failed for document {document_id}: {e}")

        # 3. 把指向此文件的背景任務 document_id 置 NULL（保留任務歷史，避免 FK 違規）
        try:
            self.db.query(models.BackgroundTask).filter(
                models.BackgroundTask.document_id == document_id
            ).update({models.BackgroundTask.document_id: None}, synchronize_session=False)
        except Exception as e:
            logger.warning(f"BackgroundTask detach failed for document {document_id}: {e}")

        # 3b. 清該文件的使用者筆記（document_id 為 NOT NULL，無法置 NULL）
        try:
            self.db.query(models.DocumentNote).filter(
                models.DocumentNote.document_id == document_id
            ).delete(synchronize_session=False)
        except Exception as e:
            logger.warning(f"DocumentNote cleanup failed for document {document_id}: {e}")

        # 4. 刪 chunks 與 document 本體，一次 commit（DB 先落地）
        self.db.query(models.DocumentChunk).filter(
            models.DocumentChunk.document_id == document_id
        ).delete(synchronize_session=False)
        self.db.delete(document)
        self.db.commit()

        # 5. DB 成功後才刪 FAISS 向量（失敗只是殘留，會被檢索過濾，不影響正確性）
        if existing_faiss_ids:
            try:
                vector_store.remove_embeddings(existing_faiss_ids)
            except Exception as e:
                logger.warning(f"FAISS removal failed for document {document_id}: {e}")

        # 6. 最後刪 PDF 檔案（使用安全的路徑驗證）
        if pdf_path:
            from ..utils.security import safe_file_delete
            try:
                safe_file_delete(pdf_path, settings.FILE_STORAGE_DIR)
            except Exception as e:
                logger.warning(f"Failed to delete PDF file {pdf_path}: {e}")

    # ---- ???? ----
    def _metadata_match(self, metadata: Dict[str, Any], filters: Dict[str, Any]) -> bool:
        for key, expected in filters.items():
            value = metadata.get(key)
            if value is None:
                return False
            if isinstance(expected, list):
                if isinstance(value, list):
                    if not set(expected).issubset(set(value)):
                        return False
                else:
                    return False
            else:
                if isinstance(value, list):
                    if expected not in value:
                        return False
                elif value != expected:
                    return False
        return True

    def _resolve_temp_pdf(self, pdf_temp_path: str) -> Path:
        """把呼叫端傳入的暫存路徑安全解析成「暫存目錄內的 .pdf」。

        Audit C2：舊版只檢查 candidate.exists()，任何絕對路徑（.env / DB / 系統檔）
        都會被接受並被 shutil.move 搬進可下載目錄 → 任意檔案外洩 / 破壞。
        修法：只取檔名、強制以暫存目錄重組，並用 validate_file_path 再確認路徑
        確實落在暫存目錄內、且副檔名為 .pdf。
        """
        from ..utils.security import validate_file_path

        name = Path(pdf_temp_path).name  # 丟棄任何目錄成分（含 ../）
        if not name or not name.lower().endswith(".pdf"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only .pdf temporary files are allowed",
            )
        candidate = self._pdf_temp / name
        try:
            return validate_file_path(candidate, self._pdf_temp, check_exists=True)
        except HTTPException as exc:
            # 統一成 400，避免洩漏是路徑不合法還是檔案不存在
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Temporary PDF file not found or already removed",
            ) from exc

    def _finalize_pdf_and_index(
        self,
        document: models.Document,
        pdf_temp_path: str,
        segments: Optional[List[Dict[str, Any]]] = None,
        original_filename: Optional[str] = None,
        force_vision: bool = False,
        force_ocr: bool = False,
    ) -> None:
        temp_path = self._resolve_temp_pdf(pdf_temp_path)

        # Construct final filename: [ID]_[OriginalName] or [ID].pdf
        if original_filename:
            # Sanitize filename
            safe_name = "".join(c for c in original_filename if c.isalnum() or c in "._- ")
            final_name = f"{document.id}_{safe_name}"
            if not final_name.lower().endswith(".pdf"):
                final_name += ".pdf"
        else:
            final_name = f"{document.id}.pdf"

        final_path = self._pdf_storage / final_name
        final_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(temp_path), final_path)
        document.pdf_path = str(final_path)
        self.db.commit()

        self._rebuild_document_chunks(
            document,
            final_path,
            segments=segments,
            force_vision=force_vision,
            force_ocr=force_ocr,
        )

    def _rebuild_document_chunks(
        self,
        document: models.Document,
        pdf_path: Path,
        segments: Optional[List[Dict[str, Any]]] = None,
        force_vision: bool = False,
        force_ocr: bool = False,
        ocr_engine: Optional[str] = None,
        ocr_tier: Optional[str] = None,
    ) -> None:
        # Audit C4：「先建後換」。舊版在這裡就先刪掉舊 chunks+向量並 commit，之後任一步
        # （OCR/VL/embedding）失敗，文件就變 0 chunks 且原文不可得（永久資料遺失）。
        # 改為：只「記住」舊 chunks 的 faiss_id，等新內容 extract + embed 成功後，
        # 才在同一交易內刪舊、加新（見下方 swap 區塊）。extract 失敗時舊資料完好無損。
        existing_chunks = (
            self.db.query(models.DocumentChunk)
            .filter(models.DocumentChunk.document_id == document.id)
            .all()
        )
        old_faiss_ids = [chunk.faiss_id for chunk in existing_chunks if chunk.faiss_id]

        # 強制 OCR 時即使是文字型 PDF 也走 PaddleOCR；忽略前端傳來的 segments
        if force_ocr:
            segments = None

        # 如果已經有 segments，就不需要重複提取
        text = None
        if segments is None:
            if document.is_image_based or force_ocr:
                document.ocr_status = "processing"
                document.ocr_method = "paddleocr"
                self.db.commit()

                try:
                    ocr_blocks = extract_image_pdf_blocks(
                        str(pdf_path), engine=ocr_engine, tier=ocr_tier
                    )
                    segments = [
                        {
                            "page": block.get("page"),
                            "paragraph_index": block.get("paragraph_index"),
                            "text": block.get("markdown") or block.get("html") or block.get("text", ""),
                        }
                        for block in ocr_blocks
                        if (block.get("markdown") or block.get("html") or block.get("text"))
                    ]
                    text = "\n\n".join(seg.get("text", "") for seg in segments if seg.get("text"))
                    document.content = text[:20000] if text else None
                    document.ocr_status = "completed" if segments else "failed"
                    self.db.commit()
                except Exception:
                    document.ocr_status = "failed"
                    self.db.commit()
                    raise
            elif force_vision:
                # 強制用 VL 視覺模型逐頁解析
                from .pdf_image import get_pdf_page_count, pdf_pages_to_images
                document.ocr_method = "vision"
                self.db.commit()
                total_pages = get_pdf_page_count(str(pdf_path))
                page_numbers = list(range(1, total_pages + 1))
                # max_dimension=768 keeps image tokens well under Ollama's 4096 ctx —
                # avoids qwen2.5vl GGML_ASSERT bug in Ollama 0.13.x+ when tokens overflow ctx.
                image_bytes_list = pdf_pages_to_images(str(pdf_path), page_numbers, dpi=80, max_dimension=768)
                segments = ai.extract_text_with_vision(image_bytes_list, page_numbers)
                text = "\n\n".join(s.get("text", "") for s in segments if s.get("text"))
                if document.content is None or not document.content.strip():
                    document.content = text[:20000] if text else None
                self.db.commit()
            else:
                # 沒有提供 segments，需要從 PDF 提取
                pdf_bytes = pdf_path.read_bytes()
                text, segments = extract_text_and_segments(pdf_bytes)
                if document.content is None or not document.content.strip():
                    document.content = text[:20000] if text else None
        else:
            # segments 已提供，完全不需要重複提取 PDF
            # content 應該在 create 時就已經設置（從 upload 回傳的 text）
            if document.content is None or not document.content.strip():
                # 如果真的沒有 content，從 segments 組合出文字
                text = "\n".join(seg.get("text", "") for seg in segments if seg.get("text"))
                document.content = text[:20000] if text else None
            else:
                # content 已有值，使用它
                text = document.content

        # 從數據庫獲取向量配置
        config_service = SystemConfigService(self.db)
        vector_config = config_service.get_vector_config()

        chunk_payloads = split_segments_into_chunks(
            segments,
            max_chars=vector_config["max_chars"],
            overlap_chars=vector_config["overlap_chars"]
        )
        if not chunk_payloads and text:
            # Fallback：如果沒有 chunk_payloads，使用整段文字（截取到 max_chars）
            chunk_payloads = [
                {
                    "chunk_index": 0,
                    "page": None,
                    "paragraph_index": None,
                    "text": text[:vector_config["max_chars"]],
                }
            ]

        # Audit H4：先算 embedding 成功，才把 chunks 寫進 DB。
        # 舊版先 commit 空 embedding 的 chunks 再 embed，embed 失敗就留下
        # 「DB 有文字、FAISS 無向量」的半成品文件（向量檢索永遠找不到、還被 BM25 索引）。
        specs = [(payload, _generate_faiss_id()) for payload in chunk_payloads]
        if not specs:
            self.db.commit()
            self.db.refresh(document, attribute_names=["chunks"])
            return

        embeddings = ai.embed_texts([payload["text"] for payload, _ in specs])
        if len(embeddings) != len(specs):
            raise RuntimeError("Embedding count does not match chunk count")

        # Audit C4 swap：到這裡 extract + embed 都已成功，現在才刪舊 chunks、加新 chunks，
        # 兩者在同一交易內一次 commit（DB 原子替換）。前面任何一步失敗都不會走到這裡，
        # 舊資料因此完好保留 —— 不再有「先刪後建，中途失敗 → 內容永久消失」。
        if existing_chunks:
            self.db.query(models.DocumentChunk).filter(
                models.DocumentChunk.document_id == document.id
            ).delete(synchronize_session=False)

        faiss_mapping: Dict[int, List[float]] = {}
        for (payload, faiss_id), embedding in zip(specs, embeddings):
            chunk = models.DocumentChunk(
                document_id=document.id,
                chunk_index=payload["chunk_index"],
                page=payload.get("page"),
                paragraph_index=payload.get("paragraph_index"),
                text=payload["text"],
                embedding=embedding,
                faiss_id=faiss_id,
            )
            self.db.add(chunk)
            faiss_mapping[faiss_id] = embedding

        # DB 先落地（刪舊+加新 一次 commit），成功後才動 FAISS：先移除舊向量、再加入新向量。
        self.db.commit()
        self.db.refresh(document, attribute_names=["chunks"])
        if old_faiss_ids:
            vector_store.remove_embeddings(old_faiss_ids)
        vector_store.add_embeddings(faiss_mapping)

    def _re_embed_existing_chunks(self, document: models.Document) -> None:
        """
        重新向量化現有的 chunks，不重新提取 PDF 文字

        這個方法只重新生成 embeddings，保留現有的文字內容
        適用於：切換 embedding 模型、清空向量值後重建
        """
        # 獲取現有的 chunks
        existing_chunks = (
            self.db.query(models.DocumentChunk)
            .filter(models.DocumentChunk.document_id == document.id)
            .order_by(models.DocumentChunk.chunk_index)
            .all()
        )

        if not existing_chunks:
            raise ValueError("此文件沒有現有的文字塊，無法重新向量化")

        # 從 FAISS 中刪除舊的 embeddings
        existing_faiss_ids = [chunk.faiss_id for chunk in existing_chunks if chunk.faiss_id]
        if existing_faiss_ids:
            vector_store.remove_embeddings(existing_faiss_ids)

        # 清空所有 chunks 的 embeddings
        for chunk in existing_chunks:
            chunk.embedding = []
        self.db.commit()

        # 重新生成 embeddings（使用現有的文字內容）
        embeddings = ai.embed_texts([chunk.text for chunk in existing_chunks])
        if len(embeddings) != len(existing_chunks):
            raise RuntimeError("Embedding count does not match chunk count")

        # 更新 chunks 和 FAISS 索引
        faiss_mapping: Dict[int, List[float]] = {}
        for chunk, embedding in zip(existing_chunks, embeddings):
            chunk.embedding = embedding
            faiss_mapping[chunk.faiss_id] = embedding
        self.db.commit()

        vector_store.add_embeddings(faiss_mapping)


# ── 背景任務執行函式（獨立 DB Session）────────────────────────────────────────

def run_document_finalize_task(
    task_id: str,
    document_id: str,
    pdf_temp_path: str,
    force_vision: bool,
    segments_json: Optional[str],
    original_filename: Optional[str],
    force_ocr: bool = False,
) -> None:
    """
    供 FastAPI BackgroundTasks 呼叫：移動 PDF 並建立向量索引。
    使用獨立 DB Session，不依賴請求生命週期。
    force_vision=True 時改用 VL 視覺模型解析；force_ocr=True 時強制走 PaddleOCR。
    """
    import json as _json
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        task = db.query(models.BackgroundTask).filter_by(id=task_id).first()
        document = db.query(models.Document).filter_by(id=document_id).first()

        if not task or not document:
            return

        task.status = "running"
        if force_ocr:
            task.message = "正在使用 PaddleOCR 辨識..."
        elif force_vision:
            task.message = "正在使用 VL 視覺模型解析..."
        else:
            task.message = "正在建立向量索引..."
        db.commit()

        service = DocumentService(db)
        segments = _json.loads(segments_json) if segments_json else None

        service._finalize_pdf_and_index(
            document,
            pdf_temp_path,
            segments=segments,
            original_filename=original_filename,
            force_vision=force_vision,
            force_ocr=force_ocr,
        )

        task.status = "completed"
        task.progress = 100
        if force_ocr:
            task.message = "PaddleOCR 辨識與向量化完成"
        elif force_vision:
            task.message = "VL 解析與向量化完成"
        else:
            task.message = "向量化完成"
        db.commit()

        # 自動觸發 KG 抽取（settings.KG_AUTO_EXTRACT=True 時）。
        # 透過 kg_queue 序列化：所有 KG 抽取共用單一 worker，一次只跑一個，
        # 避免多份文件快速上傳時並行寫入 SQLite 造成 `database is locked`。
        # 失敗只記 log，不讓主任務變 failed。
        try:
            from ..core.config import settings as _settings
            if getattr(_settings, "KG_AUTO_EXTRACT", True):
                from . import kg_queue as _kg_queue
                kg_task = models.BackgroundTask(
                    task_type="kg_extract",
                    status="pending",
                    progress=0,
                    message="已排入 KG 佇列...",
                    document_id=document_id,
                    creator_id=document.creator_id,
                )
                db.add(kg_task)
                db.commit()
                db.refresh(kg_task)
                _kg_queue.enqueue(kg_task.id, document_id)
        except Exception as kg_exc:
            import logging
            logging.getLogger(__name__).warning(
                "Auto KG extraction failed for document %s: %s", document_id, kg_exc
            )

    except Exception as exc:
        try:
            task = db.query(models.BackgroundTask).filter_by(id=task_id).first()
            if task:
                task.status = "failed"
                task.error = str(exc)[:500]
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


# 保留舊名稱相容性
run_vl_vectorize_task = run_document_finalize_task


def run_document_reocr_task(
    task_id: str,
    document_id: str,
    ocr_engine: str = "rapid",
    ocr_tier: str = "server",
) -> None:
    """背景重跑單份文件的 OCR（高精度：預設 engine=rapid, tier=server），重建 chunks + 重新向量化。

    用獨立 DB Session；對既有 pdf_path 重跑，不需重新上傳。
    """
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        task = db.query(models.BackgroundTask).filter_by(id=task_id).first()
        document = db.query(models.Document).filter_by(id=document_id).first()
        if not task or not document:
            return

        pdf_path = getattr(document, "pdf_path", None)
        if not pdf_path or not Path(pdf_path).exists():
            task.status = "failed"
            task.error = "找不到原始 PDF，無法重跑 OCR"
            db.commit()
            return

        task.status = "running"
        task.message = f"正在以 {ocr_engine}/{ocr_tier} 高精度重跑 OCR..."
        db.commit()

        service = DocumentService(db)
        service._rebuild_document_chunks(
            document,
            Path(pdf_path),
            segments=None,
            force_ocr=True,
            ocr_engine=ocr_engine,
            ocr_tier=ocr_tier,
        )

        task.status = "completed"
        task.progress = 100
        task.message = f"高精度 OCR（{ocr_engine}/{ocr_tier}）重跑與向量化完成"
        db.commit()
    except Exception as exc:
        try:
            task = db.query(models.BackgroundTask).filter_by(id=task_id).first()
            if task:
                task.status = "failed"
                task.error = str(exc)[:500]
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


def run_upload_analysis_task(task_id: str, file_path: str, filename: str) -> None:
    """
    背景執行 PDF 文字提取 + AI 建議分析。
    結果存入 BackgroundTask.result，前端 poll 後取用。
    """
    from ..database import SessionLocal
    from . import ai
    from .metadata import MetadataService
    from .pdf_processing import extract_text_and_segments, suggest_keywords

    db = SessionLocal()
    try:
        task = db.query(models.BackgroundTask).filter_by(id=task_id).first()
        if not task:
            return

        task.status = "running"
        task.progress = 15
        task.message = "正在提取 PDF 文字..."
        db.commit()

        pdf_bytes = Path(file_path).read_bytes()
        text, segments = extract_text_and_segments(pdf_bytes)

        # 判斷是否為圖片型 PDF
        text_length = len(text.strip()) if text else 0
        is_image_based = text_length < 100

        if is_image_based:
            from .pdf_image import get_pdf_page_count
            try:
                total_pages = get_pdf_page_count(file_path)
            except Exception:
                total_pages = None
            task.result = {
                "text": text or "",
                "filename": filename,
                "segments": [],
                "suggested_metadata": {},
                "suggestion": {},
                "pdf_temp_path": file_path,
                "is_image_based": True,
                "total_pages": total_pages,
            }
            task.status = "completed"
            task.progress = 100
            task.message = "偵測到圖片型 PDF"
            db.commit()
            return

        task.progress = 45
        task.message = "正在生成 AI 摘要與建議..."
        db.commit()

        # 取得分類與專案供 AI 參考
        metadata_service = MetadataService(db)
        metadata_fields = metadata_service.list_fields(active_only=True)

        classifications = db.query(models.ClassificationCategory).filter_by(is_active=True).all()
        classification_prompt = [
            f"{c.name} ({c.code})" if c.code else c.name for c in classifications
        ]

        project_field = next((f for f in metadata_fields if f.name == "project_id"), None)
        project_options = getattr(project_field, "options", []) if project_field else []
        project_prompt = [o.display_value or o.value for o in project_options]

        text_for_ai = text[:20000] if text else ""
        segs_for_ai = [s.model_dump() for s in segments]

        try:
            suggestion_payload = ai.generate_document_suggestion(
                text=text_for_ai,
                classifications=classification_prompt,
                projects=project_prompt,
                segments=segs_for_ai,
            )
        except Exception:
            suggestion_payload = {}

        task.progress = 80
        task.message = "整理關鍵字與元數據..."
        db.commit()

        # 關鍵字合併
        kw_candidates = list(suggestion_payload.get("keywords", [])) + suggest_keywords(text or "")
        seen = set()
        merged_keywords = []
        for k in kw_candidates:
            if k and k.lower() not in seen:
                seen.add(k.lower())
                merged_keywords.append(k)

        suggested_metadata: Dict[str, Any] = {"keywords": merged_keywords}

        # 比對 file_type
        file_type_field = next((f for f in metadata_fields if f.name == "file_type"), None)
        if file_type_field:
            raw_ft = (suggestion_payload.get("metadata") or {}).get("file_type")
            for opt in getattr(file_type_field, "options", []):
                if raw_ft and raw_ft in (opt.value, opt.display_value):
                    suggested_metadata["file_type"] = opt.value
                    break

        # 比對 project
        if project_field:
            raw_proj = (suggestion_payload.get("metadata") or {}).get("project_id") or suggestion_payload.get("project")
            for opt in project_options:
                if raw_proj and raw_proj in (opt.value, opt.display_value):
                    suggested_metadata["project_id"] = opt.value
                    break

        task.result = {
            "text": text_for_ai,
            "filename": filename,
            "segments": segs_for_ai,
            "suggested_metadata": suggested_metadata,
            "suggestion": suggestion_payload or {},
            "pdf_temp_path": file_path,
            "is_image_based": False,
        }
        task.status = "completed"
        task.progress = 100
        task.message = "PDF 分析完成"
        db.commit()

    except Exception as exc:
        try:
            task = db.query(models.BackgroundTask).filter_by(id=task_id).first()
            if task:
                task.status = "failed"
                task.error = str(exc)[:500]
                db.commit()
        except Exception:
            pass
    finally:
        db.close()
