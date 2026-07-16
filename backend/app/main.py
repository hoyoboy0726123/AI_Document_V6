import logging
from pathlib import Path
from sqlalchemy import inspect, text, Column, String, Integer, JSON
from fastapi import FastAPI, Request
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from . import models
from .utils.logging_config import setup_logging, get_logger

# Setup structured logging
setup_logging(
    log_level="DEBUG",
    log_file=Path("logs/app.log"),
    enable_colors=True
)
logger = get_logger(__name__)

from .api.v1 import admin, agent, auth, documents, folders, kg, metadata, rag, tasks, vector_search
from .core.config import settings
from .database import SessionLocal, engine
from .services import users as user_service


def run_db_migrations() -> bool:
    """Audit M：以 Alembic 管理 schema，取代啟動時 ad-hoc DDL。

    程式化執行 `alembic upgrade head`（baseline migration 0001 對新/舊 DB 皆冪等）。
    回傳 True 表示成功；False 表示 alembic 不可用（如 frozen build 未打包 alembic/），
    呼叫端會退回 legacy 路徑（create_all + ensure_schema_updates）。
    未來的 schema 變更請新增 alembic revision，不要再往 ensure_schema_updates 加 ALTER。
    """
    import os
    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ini_path = os.path.join(backend_dir, "alembic.ini")
    script_dir = os.path.join(backend_dir, "alembic")
    if not (os.path.isfile(ini_path) and os.path.isdir(script_dir)):
        logger.warning("Alembic files not found (%s); falling back to legacy schema path", ini_path)
        return False
    try:
        from alembic import command
        from alembic.config import Config as AlembicConfig
        cfg = AlembicConfig(ini_path)
        cfg.set_main_option("script_location", script_dir)
        cfg.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
        command.upgrade(cfg, "head")
        logger.info("Alembic migrations applied (head)")
        return True
    except Exception as exc:
        logger.error("Alembic migration failed: %s — falling back to legacy schema path", exc)
        return False


def ensure_schema_updates() -> None:
    """
    Safely update database schema using SQLAlchemy Inspector.

    LEGACY FALLBACK ONLY（audit M）：正式路徑已改為 Alembic（run_db_migrations）。
    僅在 alembic 檔案缺失（如 frozen build 未打包）或 migration 失敗時使用。
    新的 schema 變更請寫 alembic revision，不要再往這裡加 ALTER。
    """
    try:
        inspector = inspect(engine)

        # Check documents table columns
        doc_columns = {col['name'] for col in inspector.get_columns('documents')}

        with engine.begin() as connection:
            if "pdf_path" not in doc_columns:
                logger.info("Adding pdf_path column to documents table")
                # Using parameterized Column definitions (safer than raw SQL)
                connection.execute(text("ALTER TABLE documents ADD COLUMN pdf_path VARCHAR(512)"))

            # 添加 full_analysis 欄位（用於整份文件 AI 分析）
            if "full_analysis" not in doc_columns:
                logger.info("Adding full_analysis column to documents table")
                # Note: JSON column may need different syntax for different databases
                if engine.dialect.name == 'sqlite':
                    connection.execute(text("ALTER TABLE documents ADD COLUMN full_analysis TEXT"))
                else:  # PostgreSQL
                    connection.execute(text("ALTER TABLE documents ADD COLUMN full_analysis JSON"))

            # 添加 ai_summary 欄位（用於存儲 AI 自動生成的文件摘要）
            if "ai_summary" not in doc_columns:
                logger.info("Adding ai_summary column to documents table")
                connection.execute(text("ALTER TABLE documents ADD COLUMN ai_summary TEXT"))

            # 添加 OCR 相關欄位（用於處理圖片型PDF）
            if "is_image_based" not in doc_columns:
                logger.info("Adding is_image_based column to documents table")
                connection.execute(text("ALTER TABLE documents ADD COLUMN is_image_based BOOLEAN DEFAULT 0 NOT NULL"))

            if "ocr_status" not in doc_columns:
                logger.info("Adding ocr_status column to documents table")
                connection.execute(text("ALTER TABLE documents ADD COLUMN ocr_status VARCHAR(20) DEFAULT 'not_needed' NOT NULL"))

            if "ocr_method" not in doc_columns:
                logger.info("Adding ocr_method column to documents table")
                connection.execute(text("ALTER TABLE documents ADD COLUMN ocr_method VARCHAR(50)"))

            if "folder_id" not in doc_columns:
                logger.info("Adding folder_id column to documents table")
                connection.execute(text("ALTER TABLE documents ADD COLUMN folder_id VARCHAR(36) REFERENCES folders(id)"))

        # Check document_chunks table columns
        if inspector.has_table('document_chunks'):
            chunk_columns = {col['name'] for col in inspector.get_columns('document_chunks')}

            with engine.begin() as connection:
                if "faiss_id" not in chunk_columns:
                    logger.info("Adding faiss_id column to document_chunks table")
                    connection.execute(text("ALTER TABLE document_chunks ADD COLUMN faiss_id INTEGER"))

                    # Backfill faiss_id values using parameterized query
                    rows = connection.execute(text("SELECT id FROM document_chunks")).fetchall()
                    for index, (chunk_id,) in enumerate(rows, start=1):
                        connection.execute(
                            text("UPDATE document_chunks SET faiss_id = :faiss WHERE id = :chunk_id"),
                            {"faiss": index, "chunk_id": chunk_id},
                        )

        # Check background_tasks table columns
        if inspector.has_table('background_tasks'):
            bg_columns = {col['name'] for col in inspector.get_columns('background_tasks')}
            with engine.begin() as connection:
                if 'result' not in bg_columns:
                    logger.info("Adding result column to background_tasks table")
                    if engine.dialect.name == 'sqlite':
                        connection.execute(text("ALTER TABLE background_tasks ADD COLUMN result TEXT"))
                    else:
                        connection.execute(text("ALTER TABLE background_tasks ADD COLUMN result JSON"))

        # Check document_notes table columns
        if inspector.has_table('document_notes'):
            note_columns = {col['name'] for col in inspector.get_columns('document_notes')}
            with engine.begin() as connection:
                if "user_id" not in note_columns:
                    logger.info("Adding user_id column to document_notes table")
                    connection.execute(text("ALTER TABLE document_notes ADD COLUMN user_id VARCHAR(36) REFERENCES users(id)"))

        logger.info("Schema updates completed successfully")

    except Exception as e:
        logger.error(f"Schema update failed: {e}")
        # Don't raise - allow app to start even if schema update fails
        # This prevents blocking startup in case of minor issues


def ensure_default_admin() -> None:
    username = settings.DEFAULT_ADMIN_USERNAME
    password = settings.DEFAULT_ADMIN_PASSWORD
    email = settings.DEFAULT_ADMIN_EMAIL

    if not username or not password or not email:
        return

    db = SessionLocal()
    try:
        existing = user_service.get_user_by_username(db, username)
        if existing:
            updated = False
            if existing.role != "admin":
                existing.role = "admin"
                updated = True

            if not user_service.verify_password(password, existing.hashed_password):
                existing.hashed_password = user_service.hash_password(password)
                updated = True

            if updated:
                db.commit()
                print(f"[init] Default admin '{username}' updated.")
            return

        hashed_password = user_service.hash_password(password)
        admin_user = models.User(
            username=username,
            email=email,
            hashed_password=hashed_password,
            role="admin",
            is_active=True,
        )
        db.add(admin_user)
        db.commit()
        print(f"[init] Default admin '{username}' created.")
    except Exception as exc:  # pragma: no cover
        db.rollback()
        print(f"[init] Failed to create default admin: {exc}")
    finally:
        db.close()


def apply_llm_overrides_from_db() -> None:
    """
    Copy LLM/embedding provider settings from system_configs into the in-memory
    settings object, so the provider factory sees DB values without DB access.
    Called on startup; admin endpoint also mutates `settings` directly on save.
    """
    try:
        from .services.system_config import SystemConfigService

        db = SessionLocal()
        try:
            svc = SystemConfigService(db)
            overrides = svc.get_llm_provider_config()
            mapping = {
                "llm.provider": "LLM_PROVIDER",
                "llm.model": "LLM_MODEL",
                "embedding.provider": "EMBEDDING_PROVIDER",
                "embedding.model": "EMBEDDING_MODEL",
                "vision.provider": "VISION_PROVIDER",
                "gemini.api_key": "GEMINI_API_KEY",
            }
            for k, attr in mapping.items():
                if k in overrides:
                    try:
                        setattr(settings, attr, overrides[k])
                    except Exception:
                        pass
            # VL 模型同步改 OLLAMA_VISION_MODEL,讓現有 services/ai.py 直接吃到
            if "vision.model" in overrides and overrides["vision.model"]:
                try:
                    setattr(settings, "VISION_MODEL", overrides["vision.model"])
                    setattr(settings, "OLLAMA_VISION_MODEL", overrides["vision.model"])
                except Exception:
                    pass
            # LLM / Embedding 模型同步到 legacy OLLAMA_* 設定。
            # services/ai.py 等舊呼叫點直接用 ollama_client.get_client()(讀 OLLAMA_LLM_MODEL /
            # OLLAMA_EMBED_MODEL),不走 llm_provider 抽象。admin 改模型時若不同步,舊路徑
            # (如 RAG 答案生成)會繼續用 .env 的舊模型而失敗。僅在 provider=ollama 時鏡像。
            try:
                if overrides.get("llm.provider", "ollama") == "ollama" and overrides.get("llm.model"):
                    setattr(settings, "OLLAMA_LLM_MODEL", overrides["llm.model"])
                if overrides.get("embedding.provider", "ollama") == "ollama" and overrides.get("embedding.model"):
                    setattr(settings, "OLLAMA_EMBED_MODEL", overrides["embedding.model"])
            except Exception:
                pass
        finally:
            db.close()
    except Exception as exc:
        logger.warning("apply_llm_overrides_from_db failed: %s", exc)


def apply_ocr_overrides_from_db() -> None:
    """Copy OCR engine settings from system_configs into in-memory settings on startup."""
    try:
        from .services.system_config import SystemConfigService

        db = SessionLocal()
        try:
            overrides = SystemConfigService(db).get_ocr_config()
            mapping = {
                "ocr.engine": "OCR_ENGINE",
                "ocr.model_tier": "OCR_MODEL_TIER",
                "ocr.version": "OCR_VERSION",
                "ocr.device": "OCR_DEVICE",
            }
            for k, attr in mapping.items():
                if k in overrides:
                    try:
                        setattr(settings, attr, overrides[k])
                    except Exception:
                        pass
        finally:
            db.close()
    except Exception as exc:
        logger.warning("apply_ocr_overrides_from_db failed: %s", exc)


# Audit M：schema 由 Alembic 管理；alembic 不可用時才退回 legacy 路徑。
if not run_db_migrations():
    models.Base.metadata.create_all(bind=engine)
    ensure_schema_updates()
ensure_default_admin()
apply_llm_overrides_from_db()
apply_ocr_overrides_from_db()

# Start the single serial KG worker (KG extraction is funneled through it to avoid
# concurrent SQLite writers; see services/kg_queue.py).
from .services import kg_queue as _kg_queue
_kg_queue.start_worker()


def recover_stale_tasks() -> None:
    """Audit C5：程序重啟後，把上次沒跑完的背景任務收尾。

    - kg_extract 是可重跑的 → pending 重新入列、running/processing 也重排。
    - 其他任務（vectorize/reocr/pdf_analyze…）跑在 FastAPI BackgroundTasks，
      重啟後無法自動續跑 → 直接標 failed，讓前端 banner 停止轉圈。
    - 順帶把卡在 processing 的 document.ocr_status 重設為 failed。
    否則這些任務會永遠停在 pending/running，前端一直顯示進行中。
    """
    from .database import SessionLocal
    from . import models
    STALE = ("pending", "running", "processing")
    db = SessionLocal()
    try:
        tasks = (
            db.query(models.BackgroundTask)
            .filter(models.BackgroundTask.status.in_(STALE))
            .all()
        )
        requeued = failed = 0
        for task in tasks:
            if task.task_type == "kg_extract" and task.document_id:
                task.status = "pending"
                task.message = "重新排入 KG 佇列（服務重啟復原）"
                _kg_queue.enqueue(task.id, task.document_id)
                requeued += 1
            else:
                task.status = "failed"
                task.error = "服務重啟，此背景任務未完成，已標記為失敗，請重新觸發。"
                failed += 1
        stuck_docs = (
            db.query(models.Document)
            .filter(models.Document.ocr_status == "processing")
            .all()
        )
        for doc in stuck_docs:
            doc.ocr_status = "failed"
        db.commit()
        if tasks or stuck_docs:
            logger.info(
                "Stale-task recovery: %d kg re-queued, %d marked failed, %d docs ocr reset",
                requeued, failed, len(stuck_docs),
            )
    except Exception as exc:
        logger.warning("recover_stale_tasks failed: %s", exc)
    finally:
        db.close()


recover_stale_tasks()

# Initialize rate limiter (audit M：proxy-aware client IP key)
from .utils.ratelimit import client_ip_key as _client_ip_key
limiter = Limiter(key_func=_client_ip_key)

app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.PROJECT_VERSION,
    description="AI Document V3 API",
)

# Add rate limit error handler
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.include_router(auth.router, prefix="/api/v1/auth", tags=["Auth"])
app.include_router(documents.router, prefix="/api/v1/documents", tags=["Documents"])
app.include_router(admin.router, prefix="/api/v1/admin", tags=["Admin"])
app.include_router(metadata.router, prefix="/api/v1", tags=["Metadata"])
app.include_router(rag.router, prefix="/api/v1/rag", tags=["RAG"])
app.include_router(vector_search.router, prefix="/api/v1/vector-search", tags=["VectorSearch"])
app.include_router(tasks.router, prefix="/api/v1/tasks", tags=["Tasks"])
app.include_router(folders.router, prefix="/api/v1/folders", tags=["Folders"])
app.include_router(kg.router, prefix="/api/v1/kg", tags=["KnowledgeGraph"])
app.include_router(agent.router, prefix="/api/v1/agent", tags=["Agent"])


@app.get("/")
def read_root():
    return {"message": "Welcome to the Smart Document Management System!"}


@app.get("/health")
def health_check():
    return {"status": "ok"}
