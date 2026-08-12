import logging
import time
from typing import List
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ... import models, schemas
from ...core.config import settings
from ...core.security import get_current_admin_user
from ...database import get_db
from ...services.metadata import MetadataService
from ...services import ai, vector_store
from ...services.ollama_client import get_client
from ...services.system_config import SystemConfigService

logger = logging.getLogger(__name__)

router = APIRouter()


def _service(db: Session) -> MetadataService:
    return MetadataService(db)


@router.get("/metadata-fields", response_model=List[schemas.MetadataFieldRead])
@router.get("/metadata-fields/", response_model=List[schemas.MetadataFieldRead])
def list_metadata_fields(
    db: Session = Depends(get_db),
    current_admin=Depends(get_current_admin_user),
):
    _ = current_admin
    service = _service(db)
    fields = service.list_fields(active_only=False)

    # 對於 select 和 multi_select 欄位，動態添加所有文件中使用過的值
    for field in fields:
        if field.field_type in ("select", "multi_select"):
            # 收集所有文件中使用的值
            documents = db.query(models.Document).all()
            used_values = set()

            for doc in documents:
                if doc.metadata_data and field.name in doc.metadata_data:
                    value = doc.metadata_data.get(field.name)
                    if isinstance(value, list):
                        # multi_select 欄位
                        used_values.update(value)
                    elif value:
                        # select 欄位
                        used_values.add(value)

            # 獲取現有選項的 value
            existing_values = {opt.value for opt in field.options}

            # 為新的值創建臨時選項（這些不會存入資料庫）
            temp_options = []
            for value in sorted(used_values):
                if value and value not in existing_values:
                    # 創建臨時選項物件，用於顯示但不存入資料庫
                    temp_opt = models.MetadataOption(
                        id=f"temp_{field.name}_{value}",
                        field_id=field.id,
                        value=value,
                        display_value=value,
                        is_active=True,
                        order_index=9999
                    )
                    temp_options.append(temp_opt)

            # 將臨時選項添加到欄位的 options 中
            field.options.extend(temp_options)

    return fields


@router.post(
    "/metadata-fields",
    response_model=schemas.MetadataFieldRead,
    status_code=status.HTTP_201_CREATED,
)
@router.post(
    "/metadata-fields/",
    response_model=schemas.MetadataFieldRead,
    status_code=status.HTTP_201_CREATED,
)
def create_metadata_field(
    field: schemas.MetadataFieldCreate,
    db: Session = Depends(get_db),
    current_admin=Depends(get_current_admin_user),
):
    service = _service(db)
    created_field = service.create_field(
        name=field.name,
        display_name=field.display_name,
        field_type=field.field_type,
        is_required=field.is_required,
        created_by=current_admin,
        description=getattr(field, "description", None),
    )
    if field.options:
        for idx, opt in enumerate(field.options):
            service.add_option(
                created_field,
                value=opt.value,
                display_value=opt.display_value,
                order_index=opt.order_index or idx,
            )
        db.refresh(created_field)
    return service.get_field(created_field.id)


@router.put(
    "/metadata-fields/{field_id}",
    response_model=schemas.MetadataFieldRead,
)
@router.put(
    "/metadata-fields/{field_id}/",
    response_model=schemas.MetadataFieldRead,
)
def update_metadata_field(
    field_id: str,
    payload: schemas.MetadataFieldUpdate,
    db: Session = Depends(get_db),
    current_admin=Depends(get_current_admin_user),
):
    service = _service(db)
    field = service.get_field(field_id)
    if field is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Metadata field not found")
    service.update_field(
        field,
        display_name=payload.display_name,
        description=payload.description,
        field_type=payload.field_type,
        is_required=payload.is_required,
        is_active=payload.is_active,
        order_index=payload.order_index,
        updated_by=current_admin,
    )
    return service.get_field(field_id)


@router.delete("/metadata-fields/{field_id}", status_code=status.HTTP_204_NO_CONTENT)
@router.delete("/metadata-fields/{field_id}/", status_code=status.HTTP_204_NO_CONTENT)
def delete_metadata_field(
    field_id: str,
    db: Session = Depends(get_db),
    current_admin=Depends(get_current_admin_user),
):
    service = _service(db)
    field = service.get_field(field_id)
    if field is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Metadata field not found")
    service.delete_field(field)
    return None


@router.post(
    "/metadata-fields/{field_id}/options",
    response_model=schemas.MetadataFieldRead,
    status_code=status.HTTP_201_CREATED,
)
@router.post(
    "/metadata-fields/{field_id}/options/",
    response_model=schemas.MetadataFieldRead,
    status_code=status.HTTP_201_CREATED,
)
def add_metadata_option(
    field_id: str,
    payload: schemas.MetadataOptionCreate,
    db: Session = Depends(get_db),
    current_admin=Depends(get_current_admin_user),
):
    service = _service(db)
    field = service.get_field(field_id)
    if field is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Metadata field not found")
    _ = current_admin  # reserved for future audit
    service.add_option(
        field,
        value=payload.value,
        display_value=payload.display_value,
        order_index=payload.order_index,
    )
    return service.get_field(field_id)


@router.put(
    "/metadata-fields/options/{option_id}",
    response_model=schemas.MetadataFieldRead,
)
@router.put(
    "/metadata-fields/options/{option_id}/",
    response_model=schemas.MetadataFieldRead,
)
def update_metadata_option(
    option_id: str,
    payload: schemas.MetadataOptionUpdate,
    db: Session = Depends(get_db),
    current_admin=Depends(get_current_admin_user),
):
    service = _service(db)

    # 檢查是否為臨時選項（動態生成的關鍵字）
    if option_id.startswith("temp_"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="無法編輯動態生成的關鍵字選項。這些關鍵字來自文件的實際使用，若要修改，請編輯對應的文件。"
        )

    option = service.get_option(option_id)
    if option is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Metadata option not found")
    _ = current_admin
    service.update_option(
        option,
        display_value=payload.display_value,
        is_active=payload.is_active,
        order_index=payload.order_index,
    )
    return service.get_field(option.field_id)


@router.delete(
    "/metadata-fields/options/{option_id}",
    response_model=schemas.MetadataFieldRead,
)
@router.delete(
    "/metadata-fields/options/{option_id}/",
    response_model=schemas.MetadataFieldRead,
)
def delete_metadata_option(
    option_id: str,
    db: Session = Depends(get_db),
    current_admin=Depends(get_current_admin_user),
):
    service = _service(db)

    # 檢查是否為臨時選項（動態生成的關鍵字）
    if option_id.startswith("temp_"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="無法刪除動態生成的關鍵字選項。這些關鍵字來自文件的實際使用，若要移除，請編輯對應的文件。"
        )

    option = service.get_option(option_id)
    if option is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Metadata option not found")
    field_id = option.field_id
    _ = current_admin
    service.delete_option(option)
    return service.get_field(field_id)


# ========== Classification Management ==========


@router.get("/classifications", response_model=List[schemas.ClassificationRead])
@router.get("/classifications/", response_model=List[schemas.ClassificationRead])
def list_classifications(
    db: Session = Depends(get_db),
    current_admin=Depends(get_current_admin_user),
):
    """列出所有分類（包含停用的）"""
    _ = current_admin
    classifications = (
        db.query(models.ClassificationCategory)
        .order_by(models.ClassificationCategory.name)
        .all()
    )
    return classifications


@router.post(
    "/classifications",
    response_model=schemas.ClassificationRead,
    status_code=status.HTTP_201_CREATED,
)
@router.post(
    "/classifications/",
    response_model=schemas.ClassificationRead,
    status_code=status.HTTP_201_CREATED,
)
def create_classification(
    payload: schemas.ClassificationCreate,
    db: Session = Depends(get_db),
    current_admin=Depends(get_current_admin_user),
):
    """創建新的分類"""
    _ = current_admin

    # 檢查名稱是否已存在
    existing = (
        db.query(models.ClassificationCategory)
        .filter(models.ClassificationCategory.name == payload.name)
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="分類名稱已存在"
        )

    # 如果有 code，檢查是否重複
    if payload.code:
        existing_code = (
            db.query(models.ClassificationCategory)
            .filter(models.ClassificationCategory.code == payload.code)
            .first()
        )
        if existing_code:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="分類代碼已存在"
            )

    classification = models.ClassificationCategory(
        name=payload.name,
        code=payload.code,
        description=payload.description,
        is_active=True,
    )
    db.add(classification)
    db.commit()
    db.refresh(classification)
    return classification


@router.put("/classifications/{classification_id}", response_model=schemas.ClassificationRead)
@router.put("/classifications/{classification_id}/", response_model=schemas.ClassificationRead)
def update_classification(
    classification_id: str,
    payload: schemas.ClassificationUpdate,
    db: Session = Depends(get_db),
    current_admin=Depends(get_current_admin_user),
):
    """更新分類"""
    _ = current_admin

    classification = (
        db.query(models.ClassificationCategory)
        .filter(models.ClassificationCategory.id == classification_id)
        .first()
    )
    if not classification:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="分類不存在"
        )

    # 檢查名稱是否與其他分類重複
    if payload.name and payload.name != classification.name:
        existing = (
            db.query(models.ClassificationCategory)
            .filter(
                models.ClassificationCategory.name == payload.name,
                models.ClassificationCategory.id != classification_id
            )
            .first()
        )
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="分類名稱已存在"
            )
        classification.name = payload.name

    # 檢查代碼是否與其他分類重複
    if payload.code is not None and payload.code != classification.code:
        if payload.code:  # 只在非空時檢查
            existing_code = (
                db.query(models.ClassificationCategory)
                .filter(
                    models.ClassificationCategory.code == payload.code,
                    models.ClassificationCategory.id != classification_id
                )
                .first()
            )
            if existing_code:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="分類代碼已存在"
                )
        classification.code = payload.code

    if payload.description is not None:
        classification.description = payload.description

    if payload.is_active is not None:
        classification.is_active = payload.is_active

    db.commit()
    db.refresh(classification)
    return classification


@router.delete("/classifications/{classification_id}", status_code=status.HTTP_204_NO_CONTENT)
@router.delete("/classifications/{classification_id}/", status_code=status.HTTP_204_NO_CONTENT)
def delete_classification(
    classification_id: str,
    db: Session = Depends(get_db),
    current_admin=Depends(get_current_admin_user),
):
    """刪除分類"""
    _ = current_admin

    classification = (
        db.query(models.ClassificationCategory)
        .filter(models.ClassificationCategory.id == classification_id)
        .first()
    )
    if not classification:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="分類不存在"
        )

    # 檢查是否有文件使用此分類
    document_count = (
        db.query(models.Document)
        .filter(models.Document.classification_id == classification_id)
        .count()
    )
    if document_count > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"無法刪除：有 {document_count} 個文件使用此分類。請先移除這些文件的分類或刪除文件。"
        )

    db.delete(classification)
    db.commit()
    return None


# ========== System Configuration ==========


@router.get("/system-config", response_model=schemas.SystemConfigRead)
@router.get("/system-config/", response_model=schemas.SystemConfigRead)
def get_system_config(
    db: Session = Depends(get_db),
    current_admin=Depends(get_current_admin_user),
):
    """獲取系統配置"""
    _ = current_admin

    # 可用的 embedding 模型
    ollama_client = get_client()
    available_models = [
        item.get("model")
        for item in ollama_client.list_models()
        if item.get("model")
    ]

    current_embed_model = settings.OLLAMA_EMBED_MODEL
    current_llm_model = settings.OLLAMA_LLM_MODEL
    current_vision_model = settings.OLLAMA_VISION_MODEL

    # 統計信息
    total_documents = db.query(models.Document).count()

    # 統計有向量值的 chunks（embedding 不為空列表）
    # 使用 Python 層面過濾，確保跨數據庫兼容
    all_chunks = db.query(models.DocumentChunk).all()
    total_chunks = sum(1 for chunk in all_chunks if chunk.embedding and len(chunk.embedding) > 0)

    # 檢查 FAISS 索引是否存在
    faiss_path = Path(settings.FAISS_INDEX_PATH)
    faiss_index_exists = faiss_path.exists()

    # 獲取向量配置
    config_service = SystemConfigService(db)
    vector_config = config_service.get_vector_config()

    return schemas.SystemConfigRead(
        embedding_model=current_embed_model,
        llm_model=current_llm_model,
        vision_model=current_vision_model,
        available_models=available_models,
        ollama_version=ollama_client.version(),
        total_documents=total_documents,
        total_chunks=total_chunks,
        faiss_index_exists=faiss_index_exists,
        vector_config=vector_config
    )


@router.post("/clear-vectors")
@router.post("/clear-vectors/")
def clear_all_vectors(
    db: Session = Depends(get_db),
    current_admin=Depends(get_current_admin_user),
):
    """刪除所有向量值（清空 FAISS 索引和 chunks 的 embeddings）"""
    _ = current_admin

    try:
        # 1. 刪除 FAISS 索引文件
        faiss_path = Path(settings.FAISS_INDEX_PATH)
        if faiss_path.exists():
            faiss_path.unlink()
            logger.info("FAISS 索引文件已刪除")

        # 2. 丟棄記憶體中的 index 快取。
        #    只刪檔不夠：vector_store 會把 index 快取在模組層，快取還在的話
        #    換 embedding 模型後仍會拿到舊維度的 index，使用者按了本功能卻依然
        #    收到 dimension mismatch，非得重啟後端才生效。
        from ...services import vector_store

        vector_store.reset_index()

        # 3. 清空所有 chunks 的 embeddings
        db.query(models.DocumentChunk).update({"embedding": []})
        db.commit()

        total_chunks = db.query(models.DocumentChunk).count()

        logger.info(f"已清空 {total_chunks} 個 chunks 的 embeddings")

        return {
            "success": True,
            "message": f"成功刪除所有向量值（共 {total_chunks} 個 chunks）",
            "cleared_chunks": total_chunks
        }

    except Exception as exc:
        db.rollback()
        logger.error(f"刪除向量值失敗：{exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"刪除向量值失敗：{str(exc)}"
        )


@router.get("/llm-provider")
@router.get("/llm-provider/")
def get_llm_provider(
    db: Session = Depends(get_db),
    current_admin=Depends(get_current_admin_user),
):
    """Read current LLM provider config (DB overrides + .env defaults merged)."""
    _ = current_admin
    config_service = SystemConfigService(db)
    overrides = config_service.get_llm_provider_config()
    api_key = overrides.get("gemini.api_key") or settings.GEMINI_API_KEY or ""
    return {
        "llm_provider": overrides.get("llm.provider") or settings.LLM_PROVIDER,
        "llm_model": overrides.get("llm.model") or settings.LLM_MODEL or settings.OLLAMA_LLM_MODEL,
        "embedding_provider": overrides.get("embedding.provider") or settings.EMBEDDING_PROVIDER,
        "embedding_model": overrides.get("embedding.model") or settings.EMBEDDING_MODEL or settings.OLLAMA_EMBED_MODEL,
        "vision_provider": overrides.get("vision.provider") or settings.VISION_PROVIDER,
        "vision_model": overrides.get("vision.model") or settings.OLLAMA_VISION_MODEL,
        "gemini_api_key_set": bool(api_key),
        "gemini_api_key_preview": (api_key[:4] + "..." + api_key[-4:]) if len(api_key) > 12 else "",
        "gemini_llm_model_default": settings.GEMINI_LLM_MODEL,
        "gemini_embed_model_default": settings.GEMINI_EMBED_MODEL,
    }


@router.put("/llm-provider")
@router.put("/llm-provider/")
def update_llm_provider(
    payload: dict,
    db: Session = Depends(get_db),
    current_admin=Depends(get_current_admin_user),
):
    """Update LLM provider config. Mutates in-memory settings and forces provider rebuild."""
    _ = current_admin
    config_service = SystemConfigService(db)

    # Validate provider names
    valid_providers = {"ollama", "gemini"}
    if "llm_provider" in payload and payload["llm_provider"] not in valid_providers:
        raise HTTPException(status_code=400, detail=f"llm_provider must be one of {valid_providers}")
    if "embedding_provider" in payload and payload["embedding_provider"] not in valid_providers:
        raise HTTPException(status_code=400, detail=f"embedding_provider must be one of {valid_providers}")
    # VL 目前只支援 ollama;預留 gemini 但前端會擋
    if "vision_provider" in payload and payload["vision_provider"] not in {"ollama", "gemini"}:
        raise HTTPException(status_code=400, detail="vision_provider must be one of ollama|gemini")

    db_payload = {}
    if "llm_provider" in payload:
        db_payload["llm.provider"] = payload["llm_provider"]
    if "llm_model" in payload:
        db_payload["llm.model"] = payload["llm_model"]
    if "embedding_provider" in payload:
        db_payload["embedding.provider"] = payload["embedding_provider"]
    if "embedding_model" in payload:
        db_payload["embedding.model"] = payload["embedding_model"]
    if "vision_provider" in payload:
        db_payload["vision.provider"] = payload["vision_provider"]
    if "vision_model" in payload:
        db_payload["vision.model"] = payload["vision_model"]
    if "gemini_api_key" in payload:
        db_payload["gemini.api_key"] = payload["gemini_api_key"]

    config_service.update_llm_provider_config(db_payload)

    # Mutate in-memory settings so the next get_llm_provider() picks it up
    attr_map = {
        "llm_provider": "LLM_PROVIDER",
        "llm_model": "LLM_MODEL",
        "embedding_provider": "EMBEDDING_PROVIDER",
        "embedding_model": "EMBEDDING_MODEL",
        "vision_provider": "VISION_PROVIDER",
        "gemini_api_key": "GEMINI_API_KEY",
    }
    for k, attr in attr_map.items():
        if k in payload:
            try:
                setattr(settings, attr, payload[k] or None)
            except Exception:
                pass
    # VL 模型同步改 OLLAMA_VISION_MODEL,讓 services/ai.py 直接吃到
    if "vision_model" in payload and payload["vision_model"]:
        try:
            setattr(settings, "VISION_MODEL", payload["vision_model"])
            setattr(settings, "OLLAMA_VISION_MODEL", payload["vision_model"])
        except Exception:
            pass

    # Force rebuild on next call
    try:
        from ...services.llm_provider import invalidate as invalidate_providers
        invalidate_providers()
    except Exception as exc:
        logger.warning("provider invalidate failed: %s", exc)

    return {"ok": True}


@router.post("/llm-provider/test")
def test_llm_provider(
    db: Session = Depends(get_db),
    current_admin=Depends(get_current_admin_user),
):
    """Send a minimal probe to verify the current LLM provider is reachable."""
    _ = current_admin
    try:
        from ...services.llm_provider import get_llm_provider as _get
        prov = _get(force_rebuild=True)
        version = prov.version() or "(no version)"
        # Try a one-shot chat
        try:
            reply = prov.chat([
                {"role": "system", "content": "Reply with exactly: OK"},
                {"role": "user", "content": "ping"},
            ])
        except Exception as e:
            return {"ok": False, "provider": prov.name, "version": version, "error": str(e)[:200]}
        return {"ok": True, "provider": prov.name, "version": version, "reply": (reply or "")[:200]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@router.post("/llm-provider/test-vision")
def test_vision_provider(
    current_admin=Depends(get_current_admin_user),
):
    """
    Verify the VL model name resolves on the configured Ollama instance.
    Doesn't actually run inference (no image) — just confirms the model is pulled.
    """
    _ = current_admin
    try:
        ollama = get_client()
        target = settings.OLLAMA_VISION_MODEL
        models = [m.get("model") or m.get("name") for m in ollama.list_models() if m]
        if not models:
            return {"ok": False, "model": target, "error": "Ollama 無回應或沒有任何模型 — 確認 Ollama daemon 在跑"}
        if target in models:
            return {"ok": True, "model": target, "available": len(models)}
        # case-insensitive / tag-loose match for friendlier error
        loose = [m for m in models if m and target.split(":")[0] in m]
        return {
            "ok": False,
            "model": target,
            "error": f"模型 '{target}' 不在 Ollama 已下載列表",
            "suggestion": loose[:5] if loose else [],
            "available_count": len(models),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@router.get("/ocr-config")
@router.get("/ocr-config/")
def get_ocr_config(
    db: Session = Depends(get_db),
    current_admin=Depends(get_current_admin_user),
):
    """Read current OCR engine config (DB overrides + defaults merged)."""
    _ = current_admin
    overrides = SystemConfigService(db).get_ocr_config()
    return {
        "engine": overrides.get("ocr.engine") or settings.OCR_ENGINE,
        "model_tier": overrides.get("ocr.model_tier") or settings.OCR_MODEL_TIER,
        "version": overrides.get("ocr.version") or settings.OCR_VERSION,
        "device": overrides.get("ocr.device") or settings.OCR_DEVICE,
        "options": {
            "engine": ["rapid", "pp_structure"],
            "model_tier": ["mobile", "server"],
            "version": ["PP-OCRv4", "PP-OCRv5"],
            "device": ["cpu", "gpu"],
        },
    }


@router.put("/ocr-config")
@router.put("/ocr-config/")
def update_ocr_config(
    payload: dict,
    db: Session = Depends(get_db),
    current_admin=Depends(get_current_admin_user),
):
    """Update OCR engine config; mutates in-memory settings + invalidates rapid engine cache."""
    _ = current_admin
    valid = {
        "engine": {"rapid", "pp_structure"},
        "model_tier": {"mobile", "server"},
        "version": {"PP-OCRv4", "PP-OCRv5"},
        "device": {"cpu", "gpu"},
    }
    for k, allowed in valid.items():
        if k in payload and payload[k] not in allowed:
            raise HTTPException(status_code=400, detail=f"{k} must be one of {sorted(allowed)}")

    db_payload = {}
    attr_map = {
        "engine": ("ocr.engine", "OCR_ENGINE"),
        "model_tier": ("ocr.model_tier", "OCR_MODEL_TIER"),
        "version": ("ocr.version", "OCR_VERSION"),
        "device": ("ocr.device", "OCR_DEVICE"),
    }
    for k, (db_key, attr) in attr_map.items():
        if k in payload:
            db_payload[db_key] = payload[k]
            try:
                setattr(settings, attr, payload[k])
            except Exception:
                pass

    SystemConfigService(db).update_ocr_config(db_payload)

    # rebuild rapid engines on next OCR run with the new tier/version/device
    try:
        from ...services.rapid_ocr import invalidate as invalidate_rapid
        invalidate_rapid()
    except Exception as exc:
        logger.warning("rapid_ocr invalidate failed: %s", exc)

    return {"ok": True}


@router.get("/llm-concurrency")
@router.get("/llm-concurrency/")
def get_llm_concurrency(
    db: Session = Depends(get_db),
    current_admin=Depends(get_current_admin_user),
):
    """讀取「同時允許幾個 LLM 請求」。"""
    _ = current_admin
    stored = SystemConfigService(db).get_config("llm.max_concurrency")
    try:
        value = int(stored) if stored is not None else int(settings.OLLAMA_MAX_CONCURRENCY)
    except (TypeError, ValueError):
        value = int(settings.OLLAMA_MAX_CONCURRENCY)
    return {
        "max_concurrency": max(1, value),
        "min": 1,
        "max": 8,
        "note": ("1 = 完全序列化（小顯存機器建議值）。並行請求會讓 Ollama 改變模型的 "
                 "GPU/CPU 層切分，導致同一個問題的答案品質改變且持續不恢復。"
                 "換用能讓模型完全常駐 GPU 的顯卡後才建議調高。"),
    }


@router.put("/llm-concurrency")
@router.put("/llm-concurrency/")
def update_llm_concurrency(
    payload: dict,
    db: Session = Depends(get_db),
    current_admin=Depends(get_current_admin_user),
):
    """更新 LLM 併發上限；立即生效（ollama_client 偵測到變動會重建信號量）。"""
    _ = current_admin
    try:
        value = int(payload.get("max_concurrency"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="max_concurrency 必須是整數")
    if not 1 <= value <= 8:
        raise HTTPException(status_code=400, detail="max_concurrency 需介於 1 到 8")

    SystemConfigService(db).set_config(
        "llm.max_concurrency", value, "同時允許幾個 LLM 請求（1 = 序列化）")
    try:
        settings.OLLAMA_MAX_CONCURRENCY = value
    except Exception as exc:
        logger.warning("寫入 in-memory settings 失敗: %s", exc)
    logger.info("LLM 併發上限已改為 %d", value)
    return {"ok": True, "max_concurrency": value}


@router.get("/ui-config")
@router.get("/ui-config/")
def get_ui_config(
    db: Session = Depends(get_db),
    current_admin=Depends(get_current_admin_user),
):
    """UI 顯示旗標（目前只有知識圖譜頁面開關）。"""
    _ = current_admin
    show_kg = SystemConfigService(db).get_config("ui.show_knowledge_graph")
    return {"show_knowledge_graph": True if show_kg is None else bool(show_kg)}


@router.put("/ui-config")
@router.put("/ui-config/")
def update_ui_config(
    payload: dict,
    db: Session = Depends(get_db),
    current_admin=Depends(get_current_admin_user),
):
    """切換 UI 顯示旗標。

    「隱藏知識圖譜」只收起側邊欄入口，供內容用不到 KG 的單位保持介面乾淨。
    抽取行為完全不受影響（KG_AUTO_EXTRACT 照常於 ingest 後在背景執行），
    資料持續累積 —— 日後匯入含規範代號的文件後重新打開，全部看得到。
    """
    _ = current_admin
    if "show_knowledge_graph" not in payload:
        raise HTTPException(status_code=400, detail="缺少 show_knowledge_graph")
    value = bool(payload["show_knowledge_graph"])
    SystemConfigService(db).set_config(
        "ui.show_knowledge_graph", value, "側邊欄是否顯示知識圖譜頁面")
    logger.info("知識圖譜頁面顯示 = %s", value)
    return {"ok": True, "show_knowledge_graph": value}


@router.post("/reset-inference")
@router.post("/reset-inference/")
def reset_inference_engine(
    current_admin=Depends(get_current_admin_user),
):
    """卸載 Ollama 上所有已載入的模型，強制下一次請求乾淨重載。

    為什麼需要：qwen3:8b @ num_ctx=8192 佔 6.2GB，貼著本機可用 VRAM 的邊緣。
    模型每次重載（VL 換模、KEEP_ALIVE 到期、服務重啟）都可能落在不同的
    GPU/CPU 層配置上 —— 偶爾落進一個「答案塌成單張列表」的壞穩定點，且
    `ollama ps` 兩種狀態都顯示 100% GPU，無法從外部偵測。實測乾淨卸載後
    重載即恢復（答案回到與基準逐位元組相同）。詳見 DEPLOY_NOTES。

    卸載方式：對每個已載入模型送 keep_alive=0 的空請求（Ollama 官方的
    卸載慣例，等同 CLI 的 `ollama stop`）。生成型模型走 /api/generate，
    嵌入型模型 generate 會失敗，退而走 /api/embed。
    """
    _ = current_admin
    import httpx as _httpx

    unloaded: list = []
    failed: list = []
    try:
        with _httpx.Client(base_url=settings.OLLAMA_BASE_URL, timeout=60) as c:
            loaded = (c.get("/api/ps").json() or {}).get("models") or []
            for m in loaded:
                name = m.get("name") or m.get("model") or ""
                if not name:
                    continue
                try:
                    r = c.post("/api/generate", json={"model": name, "keep_alive": 0})
                    if r.status_code != 200:
                        r = c.post("/api/embed",
                                   json={"model": name, "input": "x", "keep_alive": 0})
                    (unloaded if r.status_code == 200 else failed).append(name)
                except Exception:
                    failed.append(name)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"連不上 Ollama：{exc}")

    logger.info("重置推論引擎：卸載 %s，失敗 %s", unloaded, failed or "無")
    return {"ok": not failed, "unloaded": unloaded, "failed": failed}


@router.put("/vector-config")
@router.put("/vector-config/")
def update_vector_config(
    config: schemas.VectorConfigUpdate,
    db: Session = Depends(get_db),
    current_admin=Depends(get_current_admin_user),
):
    """更新向量配置"""
    _ = current_admin

    try:
        config_service = SystemConfigService(db)
        config_service.update_vector_config(config.dict())

        logger.info(f"向量配置已更新：{config.dict()}")

        return {
            "success": True,
            "message": "向量配置已成功更新",
            "config": config.dict()
        }

    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc)
        )
    except Exception as exc:
        logger.error(f"更新向量配置失敗：{exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"更新向量配置失敗：{str(exc)}"
        )


# ========== RAG Prompt Management ==========

@router.get("/rag-prompt", response_model=schemas.RAGPromptRead)
@router.get("/rag-prompt/", response_model=schemas.RAGPromptRead)
def get_rag_prompt(
    db: Session = Depends(get_db),
    current_admin=Depends(get_current_admin_user),
):
    """取得目前生效的 RAG 提示詞（自訂或預設）。"""
    _ = current_admin
    return SystemConfigService(db).get_rag_prompts()


@router.put("/rag-prompt", response_model=schemas.RAGPromptRead)
@router.put("/rag-prompt/", response_model=schemas.RAGPromptRead)
def update_rag_prompt(
    payload: schemas.RAGPromptUpdate,
    db: Session = Depends(get_db),
    current_admin=Depends(get_current_admin_user),
):
    """儲存自訂 RAG 提示詞。模板需包含 {{question}} 與 {{context}} 佔位符。"""
    _ = current_admin
    svc = SystemConfigService(db)
    try:
        svc.update_rag_prompts(
            system_prompt=payload.system_prompt,
            user_template=payload.user_template,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return svc.get_rag_prompts()


@router.delete("/rag-prompt", status_code=status.HTTP_204_NO_CONTENT)
@router.delete("/rag-prompt/", status_code=status.HTTP_204_NO_CONTENT)
def reset_rag_prompt(
    db: Session = Depends(get_db),
    current_admin=Depends(get_current_admin_user),
):
    """重置 RAG 提示詞為程式碼預設值（等同從未修改過）。"""
    _ = current_admin
    SystemConfigService(db).reset_rag_prompts()
