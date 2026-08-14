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
    current_vision_model = settings.OLLAMA_VISION_MODEL
    # 文字模型要看實際生效的 provider：切到 AiHub 後仍顯示 OLLAMA_LLM_MODEL
    # 會讓人以為沒切成功（實測畫面寫 gemma4:12b、實際跑的是 GPT-OSS）。
    current_llm_provider = getattr(settings, "LLM_PROVIDER", "ollama") or "ollama"
    if current_llm_provider == "aihub":
        current_llm_model = (getattr(settings, "LLM_MODEL", None)
                             or getattr(settings, "AIHUB_MODEL", "gpt-oss"))
    else:
        current_llm_model = settings.LLM_MODEL or settings.OLLAMA_LLM_MODEL

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
        llm_provider=current_llm_provider,
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
    llm_provider = overrides.get("llm.provider") or settings.LLM_PROVIDER or "ollama"
    if llm_provider == "aihub":
        llm_model = (overrides.get("llm.model") or settings.LLM_MODEL
                     or getattr(settings, "AIHUB_MODEL", "gpt-oss"))
    else:
        llm_model = overrides.get("llm.model") or settings.LLM_MODEL or settings.OLLAMA_LLM_MODEL
    # AiHub 金鑰只從 .env 讀，不開放 UI 輸入也不寫進 DB —— 它是企業憑證，
    # 存進 system_configs 等於讓任何能讀 DB 的人拿到。這裡只回報有沒有設定。
    aihub_key = getattr(settings, "AIHUB_API_KEY", None) or ""
    return {
        "llm_provider": llm_provider,
        "llm_model": llm_model,
        # embedding / vision 固定本地 Ollama：AiHub 沒有 embedding 端點，
        # 也不吃影像輸入；換 embedding 還要重建全部向量，不該用切換鈕暴露。
        "embedding_provider": "ollama",
        "embedding_model": overrides.get("embedding.model") or settings.EMBEDDING_MODEL or settings.OLLAMA_EMBED_MODEL,
        "vision_provider": "ollama",
        "vision_model": overrides.get("vision.model") or settings.OLLAMA_VISION_MODEL,
        "aihub_api_key_set": bool(aihub_key),
        "aihub_api_key_preview": (aihub_key[:4] + "..." + aihub_key[-4:]) if len(aihub_key) > 12 else "",
        "aihub_base_url": getattr(settings, "AIHUB_BASE_URL", ""),
        "aihub_model_default": getattr(settings, "AIHUB_MODEL", "gpt-oss"),
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

    # 只有「文字模型」可切後端。embedding 換了要重建全部向量、AiHub 也沒有
    # embedding 與影像端點，因此這兩者固定 ollama，不接受 payload 覆寫。
    valid_llm_providers = {"ollama", "aihub"}
    if "llm_provider" in payload and payload["llm_provider"] not in valid_llm_providers:
        raise HTTPException(
            status_code=400,
            detail=f"llm_provider must be one of {sorted(valid_llm_providers)}")
    if payload.get("llm_provider") == "aihub" and not getattr(settings, "AIHUB_API_KEY", None):
        raise HTTPException(
            status_code=400,
            detail="尚未設定 AIHUB_API_KEY —— 請在後端 .env 填入後重啟服務")

    db_payload = {}
    if "llm_provider" in payload:
        db_payload["llm.provider"] = payload["llm_provider"]
    if "llm_model" in payload:
        db_payload["llm.model"] = payload["llm_model"]
    if "embedding_model" in payload:
        db_payload["embedding.model"] = payload["embedding_model"]
    if "vision_model" in payload:
        db_payload["vision.model"] = payload["vision_model"]
    # embedding / vision 的 provider 永遠寫回 ollama，清掉早期可能存下的 gemini
    db_payload["embedding.provider"] = "ollama"
    db_payload["vision.provider"] = "ollama"

    config_service.update_llm_provider_config(db_payload)

    # Mutate in-memory settings so the next get_llm_provider() picks it up
    attr_map = {
        "llm_provider": "LLM_PROVIDER",
        "llm_model": "LLM_MODEL",
        "embedding_model": "EMBEDDING_MODEL",
    }
    for k, attr in attr_map.items():
        if k in payload:
            try:
                setattr(settings, attr, payload[k] or None)
            except Exception:
                pass
    for attr in ("EMBEDDING_PROVIDER", "VISION_PROVIDER"):
        try:
            setattr(settings, attr, "ollama")
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
