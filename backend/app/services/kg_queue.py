"""Single-threaded serial queue for KG extraction.

All KG extraction — both auto-after-ingest (services/documents.py) and the manual
POST /kg/extract endpoint (api/v1/kg.py) — is funneled through ONE worker thread so
that only one KG extraction ever runs at a time.

Why: KG extraction is write-heavy (upserts entities + relations). The DB is SQLite,
which allows only a single writer. When several documents are ingested in quick
succession each would otherwise spawn a concurrent KG task (FastAPI BackgroundTasks
run in a threadpool), and the overlapping writes raise `database is locked` (HTTP 500).
Serialising KG through this queue removes that contention regardless of upload pace,
so KG_AUTO_EXTRACT can stay on even for rapid multi-file uploads. Tasks still appear
immediately in the background_tasks table as `pending`; they flip to running/completed
as the worker reaches them.
"""
from __future__ import annotations

import logging
import queue
import threading

logger = logging.getLogger(__name__)

_q: "queue.Queue[tuple[str, str]]" = queue.Queue()
_worker: "threading.Thread | None" = None
_lock = threading.Lock()


def _mark_task_failed(task_id: str, message: str) -> None:
    """Best-effort：把卡住的任務標成 failed，避免前端永遠轉圈（audit medium）。"""
    try:
        from ..database import SessionLocal
        from .. import models
        db = SessionLocal()
        try:
            task = db.query(models.BackgroundTask).filter(models.BackgroundTask.id == task_id).first()
            if task and task.status in ("pending", "running", "processing"):
                task.status = "failed"
                task.error = (message or "")[:2000]
                db.commit()
        finally:
            db.close()
    except Exception:
        logger.exception("KG worker: failed to mark task %s as failed", task_id)


def _run() -> None:
    while True:
        task_id, document_id = _q.get()
        try:
            # 延遲 import 放進迴圈並包 try：即使 kg_pipeline import 失敗（如打包環境缺
            # hiddenimport），worker 也不會整條死掉（audit medium：無聲重啟迴圈）。
            from . import kg_pipeline
            logger.info("KG worker: start doc=%s (remaining in queue=%d)", document_id, _q.qsize())
            kg_pipeline.run_kg_extract_task(task_id, document_id)
            logger.info("KG worker: done doc=%s", document_id)
        except Exception as exc:
            logger.exception("KG worker: extraction failed for doc=%s", document_id)
            _mark_task_failed(task_id, f"KG extraction failed: {exc}")
        finally:
            _q.task_done()


def start_worker() -> None:
    """Idempotent — ensure the single KG worker thread is alive."""
    global _worker
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, name="kg-worker", daemon=True)
            _worker.start()
            logger.info("KG serial worker started")


def enqueue(task_id: str, document_id: str) -> int:
    """Queue a KG extraction; returns the queue depth after enqueue."""
    start_worker()
    _q.put((task_id, document_id))
    depth = _q.qsize()
    logger.info("KG task queued (depth=%d) for doc=%s", depth, document_id)
    return depth


def queue_depth() -> int:
    return _q.qsize()
