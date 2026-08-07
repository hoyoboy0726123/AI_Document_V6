import time
from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ... import models, schemas
from ...core.security import get_current_user, get_current_admin_user
from ...database import get_db
from ...services import ai, vector_store

router = APIRouter()


@router.post("/test", response_model=schemas.VectorSearchTestResponse)
def test_vector_search(
    payload: schemas.VectorSearchTestRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    query = (payload.query or "").strip()
    if not query:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="查詢不可為空")

    t0 = time.time()

    embeddings = ai.embed_query(query)
    if not embeddings:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="向量化失敗")

    # 搜尋數量放大，方便後續過濾
    raw_results = vector_store.search(embeddings[0], top_k=payload.top_k * 10)

    faiss_ids = [fid for fid, _ in raw_results]
    chunk_rows = (
        db.query(models.DocumentChunk)
        .join(models.Document, models.DocumentChunk.document_id == models.Document.id)
        .filter(models.DocumentChunk.faiss_id.in_(faiss_ids))
        .all()
    )
    chunk_map = {c.faiss_id: c for c in chunk_rows}

    results = []
    rank = 1
    for fid, score in raw_results:
        if score < payload.min_score:
            continue
        chunk = chunk_map.get(fid)
        if not chunk:
            continue
        if payload.document_id and chunk.document_id != payload.document_id:
            continue

        doc = chunk.document
        results.append(
            schemas.VectorSearchTestResult(
                rank=rank,
                chunk_id=chunk.id,
                document_id=doc.id,
                document_title=doc.title,
                page=chunk.page,
                score=round(score, 4),
                text=chunk.text,
            )
        )
        rank += 1
        if rank > payload.top_k:
            break

    elapsed_ms = int((time.time() - t0) * 1000)
    return schemas.VectorSearchTestResponse(
        query=query,
        results=results,
        elapsed_ms=elapsed_ms,
    )


@router.get("/health", response_model=schemas.VectorHealthResponse)
def vector_health(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    chunks = (
        db.query(models.DocumentChunk)
        .join(models.Document, models.DocumentChunk.document_id == models.Document.id)
        .all()
    )

    TOO_SHORT = 100

    # 「過長」門檻必須跟著實際切塊設定走，不能寫死。
    # split_segments_into_chunks() 切出一塊後，下一塊會先塞入 overlap_chars 的重疊
    # 再累積到 max_chars，因此正常產出的塊最大可達 max_chars + overlap_chars。
    # 舊版寫死 1800（只等於 max_chars），導致所有帶重疊的正常塊都被誤報為異常。
    # 切塊參數可從管理介面調整（system_configs.vector_config），故在此動態取用。
    try:
        from ...services.system_config import SystemConfigService

        _vc = SystemConfigService(db).get_vector_config()
        TOO_LONG = int(_vc.get("max_chars", 1800)) + int(_vc.get("overlap_chars", 250))
    except Exception:  # 設定讀取失敗時退回保守預設，不讓健康檢查整個掛掉
        TOO_LONG = 1800 + 250

    total_chunks = len(chunks)
    total_chars = sum(len(c.text) for c in chunks)
    avg_chars_per_chunk = (total_chars // total_chunks) if total_chunks else 0
    empty_embedding_count = sum(1 for c in chunks if not c.embedding)

    # Per-document stats
    doc_map: dict = {}
    for c in chunks:
        doc = c.document
        if c.document_id not in doc_map:
            doc_map[c.document_id] = {
                "document_id": c.document_id,
                "document_title": doc.title,
                "chunk_count": 0,
                "total_chars": 0,
                "empty_embedding_count": 0,
            }
        entry = doc_map[c.document_id]
        entry["chunk_count"] += 1
        entry["total_chars"] += len(c.text)
        if not c.embedding:
            entry["empty_embedding_count"] += 1

    document_stats = [
        schemas.DocumentChunkStat(
            document_id=v["document_id"],
            document_title=v["document_title"],
            chunk_count=v["chunk_count"],
            total_chars=v["total_chars"],
            avg_chars=v["total_chars"] // v["chunk_count"] if v["chunk_count"] else 0,
            empty_embedding_count=v["empty_embedding_count"],
        )
        for v in doc_map.values()
    ]

    abnormal_chunks = []
    for c in chunks:
        char_count = len(c.text)
        if char_count < TOO_SHORT:
            reason = "too_short"
        elif char_count > TOO_LONG:
            reason = "too_long"
        else:
            continue
        abnormal_chunks.append(
            schemas.AbnormalChunk(
                chunk_id=c.id,
                document_id=c.document_id,
                document_title=c.document.title,
                page=c.page,
                char_count=char_count,
                reason=reason,
                text_preview=c.text[:120],
            )
        )

    return schemas.VectorHealthResponse(
        total_chunks=total_chunks,
        total_documents=len(doc_map),
        total_chars=total_chars,
        avg_chars_per_chunk=avg_chars_per_chunk,
        empty_embedding_count=empty_embedding_count,
        abnormal_chunks=abnormal_chunks,
        document_stats=document_stats,
    )


@router.post("/rebuild-index")
def rebuild_faiss_index(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    """從 DB 儲存的 embedding 全量重建 FAISS index（audit H2 逃生門）。

    用於 index 檔損毀、或換 embedding 模型維度改變後修復。admin only。
    """
    rows = (
        db.query(models.DocumentChunk.faiss_id, models.DocumentChunk.embedding)
        .all()
    )
    mapping = {
        fid: emb
        for fid, emb in rows
        if fid is not None and emb  # 跳過無向量（半成品）chunk
    }
    rebuilt = vector_store.rebuild_index(mapping)
    return {
        "rebuilt_vectors": rebuilt,
        "total_chunks": len(rows),
        "skipped_empty": len(rows) - len(mapping),
    }
