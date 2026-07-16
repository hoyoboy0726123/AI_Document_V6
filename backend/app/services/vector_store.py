from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Dict, List, Tuple

import faiss
import numpy as np

from ..core.config import settings

logger = logging.getLogger(__name__)

_INDEX_LOCK = threading.Lock()
_INDEX: faiss.Index | None = None
_INDEX_DIMENSION: int | None = None
_INDEX_PATH = Path(settings.FAISS_INDEX_PATH)
_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)


def _write_index_atomic(index: faiss.Index) -> None:
    """原子寫入 index 檔（audit H2）。

    舊版直接 faiss.write_index 覆寫目標檔，寫到一半斷電/被砍 → 檔案損毀，
    下次 read_index 直接拋例外，所有向量檢索死亡。改為先寫 .tmp 再 os.replace
    （同磁碟 rename 在 Windows/POSIX 皆為原子操作）。
    """
    tmp_path = _INDEX_PATH.with_suffix(_INDEX_PATH.suffix + ".tmp")
    faiss.write_index(index, str(tmp_path))
    os.replace(str(tmp_path), str(_INDEX_PATH))


def _ensure_index(dimension: int) -> faiss.Index:
    global _INDEX, _INDEX_DIMENSION
    if _INDEX is not None:
        return _INDEX

    if _INDEX_PATH.exists():
        _INDEX = faiss.read_index(str(_INDEX_PATH))
        _INDEX_DIMENSION = _INDEX.d
        return _INDEX

    index_flat = faiss.IndexFlatIP(dimension)
    _INDEX = faiss.IndexIDMap2(index_flat)
    _INDEX_DIMENSION = dimension
    return _INDEX


def add_embeddings(embeddings: Dict[int, List[float]]) -> None:
    if not embeddings:
        return

    ids = np.array(list(embeddings.keys()), dtype="int64")
    vectors = np.array(list(embeddings.values()), dtype="float32")
    dimension = vectors.shape[1]

    with _INDEX_LOCK:
        index = _ensure_index(dimension)
        # Audit H3：換 embedding 模型後維度不一致，舊版直接讓 faiss 拋難懂的錯、
        # 且已先刪舊向量造成資料遺失。這裡給明確錯誤，讓上層能中止並提示需全量重建。
        if index.d != dimension:
            raise ValueError(
                f"Embedding dimension mismatch: FAISS index expects {index.d} dimensions "
                f"but got {dimension}. The embedding model likely changed — run a full "
                f"index rebuild (rebuild_index) before adding new vectors."
            )
        index.add_with_ids(vectors, ids)
        _write_index_atomic(index)


def remove_embeddings(faiss_ids: List[int]) -> None:
    if not faiss_ids:
        return

    with _INDEX_LOCK:
        global _INDEX, _INDEX_DIMENSION
        if _INDEX is None:
            if not _INDEX_PATH.exists():
                return
            _INDEX = faiss.read_index(str(_INDEX_PATH))
            _INDEX_DIMENSION = _INDEX.d
        id_array = np.array(faiss_ids, dtype="int64")
        _INDEX.remove_ids(id_array)
        _write_index_atomic(_INDEX)


def search(embedding: List[float], top_k: int = 5) -> List[Tuple[int, float]]:
    if not embedding:
        return []

    vector = np.array([embedding], dtype="float32")

    with _INDEX_LOCK:
        global _INDEX, _INDEX_DIMENSION
        if _INDEX is None:
            if not _INDEX_PATH.exists():
                return []
            _INDEX = faiss.read_index(str(_INDEX_PATH))
            _INDEX_DIMENSION = _INDEX.d
        index = _INDEX

        if index.ntotal == 0:
            return []

        if index.d != vector.shape[1]:
            raise ValueError(
                f"Embedding dimension mismatch: index expects {index.d} dimensions, "
                f"but got {vector.shape[1]} dimensions"
            )

        scores, ids = index.search(vector, top_k)

    results: List[Tuple[int, float]] = []
    for chunk_id, score in zip(ids[0], scores[0]):
        if int(chunk_id) == -1:
            continue
        results.append((int(chunk_id), float(score)))
    return results


def rebuild_index(embeddings: Dict[int, List[float]]) -> int:
    """從 (faiss_id -> embedding) 對映「全量重建」index 檔（audit H2 逃生門）。

    用途：index 檔損毀、或換 embedding 模型維度改變後，從 DB 儲存的
    embedding JSON 重新建立一個乾淨的新 index，原子換檔。
    回傳寫入的向量數。呼叫端（admin 端點）負責從 DB 收集 embeddings。
    """
    global _INDEX, _INDEX_DIMENSION
    with _INDEX_LOCK:
        if not embeddings:
            # 空庫：建立一個空 index（維度未知時預設用 settings 或 1，僅佔位）
            dim = _INDEX_DIMENSION or int(getattr(settings, "EMBEDDING_DIMENSION", 0) or 0) or 1
            index = faiss.IndexIDMap2(faiss.IndexFlatIP(dim))
            _INDEX = index
            _INDEX_DIMENSION = dim
            _write_index_atomic(index)
            return 0

        ids = np.array(list(embeddings.keys()), dtype="int64")
        vectors = np.array(list(embeddings.values()), dtype="float32")
        dimension = vectors.shape[1]
        index = faiss.IndexIDMap2(faiss.IndexFlatIP(dimension))
        index.add_with_ids(vectors, ids)
        _INDEX = index
        _INDEX_DIMENSION = dimension
        _write_index_atomic(index)
        logger.info("FAISS index rebuilt from DB: %d vectors, dim=%d", len(embeddings), dimension)
        return len(embeddings)
