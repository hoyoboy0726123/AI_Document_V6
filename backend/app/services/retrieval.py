"""Single source of truth for RAG retrieval.

Previously the retrieval pipeline (hybrid fuse → pool → rerank) and the
small-to-big neighbor expansion existed as TWO drifting copies: one in
api/v1/rag.py (the tuned one, used by /rag/query) and a weaker one in
services/agent_tools.py (used by the agent's rag_search tool). They ranked
chunks differently, so the agent sometimes buried the right chunk. This module
is the one implementation that /rag/query, the agent seed, and the agent's
rag_search tool all call.
"""
from __future__ import annotations

import re

from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from .. import models
from ..core.config import settings
from . import ai, hybrid_search, rerank

_PAGE_GAP = 5


def _project_matches(metadata: Dict[str, object], project_id: str) -> bool:
    value = (metadata or {}).get("project_id")
    if value is None:
        return False
    if isinstance(value, list):
        return project_id in value
    return value == project_id


def _join_dedup(a: str, b: str, max_overlap: int = 400) -> str:
    """串接 a+b，並裁掉 a 結尾與 b 開頭重複的部分（相鄰塊本來就有 overlap）。"""
    if not a:
        return b
    if not b:
        return a
    limit = min(len(a), len(b), max_overlap)
    for k in range(limit, 20, -1):
        if a[-k:] == b[:k]:
            return a + b[k:]
    return a + "\n" + b


def expand_chunk_text(db: Session, chunk, *, radius: int, max_chars: int) -> str:
    """小找大：命中塊「完整保留」+ 同文件前後 radius 塊只填剩餘預算，合併去重。

    搜尋精度用小塊（向量比對的是命中塊），但餵 LLM 的是補上鄰塊的大塊，避免句子/數值
    被切塊邊界截斷。命中塊完整保留是關鍵——否則前一塊會把命中塊擠掉、害表格被截尾
    （例如查 101.78 卻只看到表格前兩列）。
    """
    base = chunk.text or ""
    if radius <= 0 or len(base) >= max_chars:
        return base[:max_chars]
    rows = (
        db.query(models.DocumentChunk)
        .filter(
            models.DocumentChunk.document_id == chunk.document_id,
            models.DocumentChunk.chunk_index >= chunk.chunk_index - radius,
            models.DocumentChunk.chunk_index <= chunk.chunk_index + radius,
        )
        .order_by(models.DocumentChunk.chunk_index)
        .all()
    )
    if len(rows) <= 1:
        return base
    hi = chunk.chunk_index
    nxt = next((r.text or "" for r in rows if r.chunk_index == hi + 1), "")
    prv = next((r.text or "" for r in rows if r.chunk_index == hi - 1), "")
    result = base
    rem = max_chars - len(result)
    if rem > 0 and nxt:
        result = _join_dedup(result, nxt[:rem])
    rem = max_chars - len(result)
    if rem > 0 and prv:
        result = _join_dedup(prv[-rem:], result)
    return result[:max_chars]


def context_text_budgeted(db: Session, chunk, ctx_used: int) -> str:
    """在「總 context 預算」內做鄰塊擴展。

    關鍵：預算用完時必須「截斷到剩餘額度」(或回空)，不能回傳整塊——否則多塊加總會
    超過 num_ctx，導致模型(如 gemma)吃到溢出的 prompt 而吐空，最後落到「暫無足夠資訊」。
    """
    base = chunk.text or ""
    total_budget = ai.effective_rag_budget()
    remaining = total_budget - ctx_used
    if remaining <= 0:
        return ""  # 預算已用完：不再塞，避免 prompt 溢出 num_ctx
    radius = getattr(settings, "RAG_NEIGHBOR_RADIUS", 1)
    per_cap = getattr(settings, "RAG_EXPAND_MAX_CHARS", 2000)
    if radius <= 0 or remaining <= len(base):
        return base[:remaining]  # 截斷到剩餘額度（原本回傳整塊 → 溢出 bug）
    return expand_chunk_text(db, chunk, radius=radius, max_chars=min(per_cap, remaining))


def context_keep_ids(filtered) -> set:
    """智慧頁距過濾 + 塌縮保護：回傳應納入 LLM context 的 chunk id 集合。

    只在「多數命中本來就集中在主命中附近」時才套頁距過濾；否則（分散/離群）全部納入，
    避免 context 塌縮到只剩 1 塊（例如最高分命中落在修訂紀錄頁）。再依 RAG_MAX_CONTEXT_CHUNKS
    取前 N（超出的塊仍會列在 sources，只是不進 LLM）。
    """
    if not filtered:
        return set()
    primary = filtered[0][0]
    pp = primary.page or 0
    pid = primary.document_id

    def _near(c):
        if c.document_id != pid or not pp or not c.page:
            return True
        return abs(c.page - pp) <= _PAGE_GAP

    near_ids = {c.id for c, _ in filtered if _near(c)}
    if len(near_ids) >= max(2, (len(filtered) + 1) // 2):
        base = near_ids
    else:
        base = {c.id for c, _ in filtered}

    cap = getattr(settings, "RAG_MAX_CONTEXT_CHUNKS", 6)
    ordered_kept = [c.id for c, _ in filtered if c.id in base][:cap]
    return set(ordered_kept)


def _get_vector_config(db: Session, vector_config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if vector_config is not None:
        return vector_config
    from .system_config import SystemConfigService
    return SystemConfigService(db).get_vector_config()


_CID_RE = re.compile(r"\(cid:\d+\)")
_UNREADABLE_CID_RATIO = 0.3


def _is_unreadable(text: Optional[str]) -> bool:
    """整塊幾乎都是 (cid:N) 代碼 → 抽取失敗的殘骸，不該進檢索結果。

    來源：PDF 字型缺少 ToUnicode 對照表時，pdfminer 會退回輸出字元代碼。
    實測 MIL-STD-1366E 有 1149 塊這種內容（佔整個向量庫 15%），中位數
    99% 的字元是 cid 代碼。它們會被向量檢索命中並擠掉真正有內容的段落
    —— 使用者查沙塵測試時，前四筆來源全是這種亂碼。

    在檢索端過濾而非刪除資料：不需重建索引，且哪天把該 PDF 重新抽取好了
    就會自動恢復可用。門檻取 0.3，正常英文段落偶爾夾雜幾個 cid 不受影響。
    """
    if not text:
        return False
    cid_chars = sum(len(m) for m in _CID_RE.findall(text))
    return cid_chars / len(text) >= _UNREADABLE_CID_RATIO


def hybrid_retrieve(
    db: Session,
    query: str,
    embedding: List[float],
    top_k: int,
    *,
    vector_config: Optional[Dict[str, Any]] = None,
    document_id: Optional[str] = None,
    classification_id: Optional[str] = None,
    project_id: Optional[str] = None,
    folder_ids: Optional[List[str]] = None,
) -> List[Tuple[object, float]]:
    """混合檢索（向量 + BM25，RRF 融合）→ 撈寬 pool → cross-encoder 精選 → top_k。

    回傳 [(chunk, score)]。keyword-only 命中（向量低分但關鍵字命中）不被向量門檻砍掉。
    過濾參數（document/classification/project/folder）全 None 時為全域檢索（agent 用）。
    """
    cfg = _get_vector_config(db, vector_config)
    candidate_k = top_k * cfg["search_multiplier"]
    fused, kw_hits = hybrid_search.fuse(db, query, embedding, candidate_k)
    if not fused:
        return []

    faiss_ids = [fid for fid, _ in fused]
    chunk_rows = (
        db.query(models.DocumentChunk)
        .join(models.Document, models.DocumentChunk.document_id == models.Document.id)
        .filter(models.DocumentChunk.faiss_id.in_(faiss_ids))
        .all()
    )
    chunk_map = {c.faiss_id: c for c in chunk_rows}
    min_sim = cfg["min_similarity_score"]

    rerank_on = getattr(settings, "RAG_RERANK", True)
    pool_size = max(top_k, getattr(settings, "RAG_RERANK_POOL", 12)) if rerank_on else top_k

    pool: List[Tuple[object, float]] = []
    seen_pages = set()
    for fid, vscore in fused:
        chunk = chunk_map.get(fid)
        if not chunk:
            continue
        if vscore is not None and vscore < min_sim and fid not in kw_hits:
            continue
        doc = chunk.document
        if document_id and doc.id != document_id:
            continue
        if classification_id and doc.classification_id != classification_id:
            continue
        if project_id and not _project_matches(doc.metadata_data or {}, project_id):
            continue
        if folder_ids and doc.folder_id not in folder_ids:
            continue
        if _is_unreadable(chunk.text):
            continue
        page_key = (doc.id, chunk.page)
        if chunk.page is not None and page_key in seen_pages:
            continue
        seen_pages.add(page_key)
        pool.append((chunk, vscore if vscore is not None else 0.0))
        if len(pool) >= pool_size:
            break

    if not rerank_on or len(pool) <= 1:
        return pool[:top_k]
    return rerank.rerank(query, pool, top_k)
