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

# context_keep_ids 的頁距視窗。
#
# 這個值用檢索層指標（recall@20 / hit@5 / MRR）永遠驗不出來 —— 它作用在
# context_keep_ids，而那些指標只量到 hybrid_retrieve 為止。必須用生成層診斷。
#
# 實測（scripts/eval/diag_generation.py，兩題「正解進了 top-5 卻沒進脈絡」）：
#     v11 鹽霧箱體溫度  top5 頁碼 [261, 255★, 259, 256, 977]  正解距主命中 6 頁
#     v16 清潔劑標準條件 top5 頁碼 [19, 20, 9, 10★, 11]        正解距主命中 9 頁
#     PAGE_GAP=5   兩題的正解都被擋在脈絡外（檢索排第 2 / 第 4 名卻沒送進 LLM）
#     PAGE_GAP=15  兩題都進得去；30 / 60 沒有進一步差別
#
# 810H 單一 Method 常跨 20+ 頁，±5 的視窗對這種語料本來就太窄。
_PAGE_GAP = 15


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


def page_gap_within_doc(
    primary_doc_id: Optional[str],
    primary_page: Optional[int],
    doc_id: Optional[str],
    page: Optional[int],
) -> Optional[int]:
    """主命中與某來源的頁距 —— 只在同一份文件內才有意義。

    頁碼是「文件內」座標：A 文件第 3 頁與 B 文件第 250 頁之間不存在「頁距 247」。
    跨文件（或頁碼/文件識別缺漏）一律回 None —— _page_label 對 None 只印頁碼、
    不加 ⚠️ 警示，提示詞的「頁距大→優先捨棄」規則便不會誤傷合法的跨文件來源。
    """
    if not (primary_doc_id and primary_page and page):
        return None
    if doc_id != primary_doc_id:
        return None
    return abs(page - primary_page)


def _get_vector_config(db: Session, vector_config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if vector_config is not None:
        return vector_config
    from .system_config import SystemConfigService
    return SystemConfigService(db).get_vector_config()


_CID_RE = re.compile(r"\(cid:\d+\)")
_UNREADABLE_CID_RATIO = 0.3

# 目錄頁的點引線（「第 3 章 .......... 15」）。
_DOT_LEADER_RE = re.compile(r"\.{5,}")
_TOC_DOT_RATIO = 0.10

# 同一頁最多納入幾塊。1（原行為）會讓 60% 的區塊永遠取不到。
_MAX_CHUNKS_PER_PAGE = 3


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
    if _CID_RE.search(text):
        cid_chars = sum(len(m) for m in _CID_RE.findall(text))
        if cid_chars / len(text) >= _UNREADABLE_CID_RATIO:
            return True

    # 目錄頁：整塊都是「章節標題 ....... 頁碼」。
    #
    # 這類區塊語意空洞，卻因為含有大量真實章節標題而向量相似度很高 —— 使用者問
    # 「沙塵測試程序」，命中的是目錄裡寫著「沙塵測試」那一行，而不是內文。
    # 實測 631 / 7558（8.35%）的區塊屬於此類；活體檢索（12 條查詢 × top-50）
    # 顯示它們佔掉 12% 的候選名額，其中 2 條查詢的第一名直接就是目錄頁。
    #
    # 門檻對曲線不敏感（>10% 得 631 塊、>50% 得 535 塊），表示這些是純目錄，
    # 不會誤傷正文中偶爾出現的省略號。
    dot_chars = sum(len(m) for m in _DOT_LEADER_RE.findall(text))
    return dot_chars / len(text) >= _TOC_DOT_RATIO


_SPEC_ID_IN_QUERY_RE = re.compile(
    r"\b(MIL-(?:STD|HDBK|DTL|PRF)-\d+[A-Z]?(?:-\d+)?|AR\s?\d+-\d+"
    r"|ASTM\s?[A-Z]?\d+|IEC\s?\d+(?:-\d+)*|ISO\s?\d+|NATO\s?STANAG\s?\d+)\b",
    re.I,
)


def resolve_spec_scope(db: Session, question: str) -> Tuple[str, Optional[str]]:
    """把查詢裡的規範編號從「嵌入用字串」移到「文件過濾條件」。

    回傳 (給 embedding 用的字串, document_id 或 None)。

    為什麼要這麼做：使用者最自然的問法是「MIL-STD-810H 沙塵測試的沙塵濃度是
    多少」，但 `MIL-STD-810H` 這串 token 在該文件的目錄頁、頁首、參考文獻頁
    出現上千次，會主導整個查詢向量，把真正的答案段落擠出候選名單。實測：

        MIL-STD-810H 沙塵測試的沙塵濃度是多少   含編號 → 正解排名 [62, 181]
        沙塵測試的沙塵濃度是多少                 去編號 → 正解排名 [1, 8, 10, 12]
        MIL-STD-810H 低壓測試的最終客艙高度      含編號 → 前 200 名內沒有正解

    候選池只有 top_k × multiplier（預設 50），排名 62 等於根本進不了池子，
    後面的 rerank 再強也救不回來。端對端實測：同一題純中文問法答得出濃度，
    加上編號就回「查無相關資料」。

    單純移除編號會失去「只有編號才指得出文件」的能力（例如「MIL-STD-461G 的
    CE102」），所以把編號改用來鎖定 document_id —— 兩者兼得。
    BM25 那一路仍拿原始字串，編號在關鍵字比對上是有用訊號。
    """
    ids = _SPEC_ID_IN_QUERY_RE.findall(question or "")
    if not ids:
        return question, None

    doc_id = None
    for raw in ids:
        norm = re.sub(r"\s+", "", raw).upper()
        for doc in db.query(models.Document.id, models.Document.title).all():
            if norm in re.sub(r"\s+", "", (doc.title or "")).upper():
                doc_id = doc.id
                break
        if doc_id:
            break

    # 找不到對應文件就不要縮限範圍（可能是語料裡沒有的外部規範），
    # 但仍把編號從嵌入字串移除 —— 它對向量檢索只有干擾。
    stripped = _SPEC_ID_IN_QUERY_RE.sub(" ", question or "")
    stripped = re.sub(r"\s+", " ", stripped).strip()
    return (stripped or question), doc_id


def resolve_spec_docs(db: Session, question: str) -> Tuple[str, List[str]]:
    """多規範版的 resolve_spec_scope：回傳 (去編號的字串, 所有命中的 document_id)。

    resolve_spec_scope 只取第一個命中的編號就 break，對「A 與 B 各是多少」
    這種比較題會整個偏向其中一份。實測「MIL-STD-810H 的溫度量測公差與
    MIL-STD-331D 的溫度量測公差各是多少」候選池 10 塊裡 9 塊來自 810H、
    0 塊來自 331D —— 系統回「查無相關資料」，因為它其實只查了一份。
    """
    ids = _SPEC_ID_IN_QUERY_RE.findall(question or "")
    doc_ids: List[str] = []
    if ids:
        docs = db.query(models.Document.id, models.Document.title).all()
        for raw in ids:
            norm = re.sub(r"\s+", "", raw).upper()
            for doc in docs:
                if norm in re.sub(r"\s+", "", (doc.title or "")).upper():
                    if doc.id not in doc_ids:
                        doc_ids.append(doc.id)
                    break
    stripped = re.sub(r"\s+", " ", _SPEC_ID_IN_QUERY_RE.sub(" ", question or "")).strip()
    return (stripped or question), doc_ids


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
    page_counts: Dict[tuple, int] = {}
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
        # 同一頁允許多塊。
        #
        # 原本一頁只留 RRF 最前面的那一塊，但語料平均一頁 2.52 塊、最多 34 塊，
        # 有 60% 的區塊在「同頁已被別人佔走」後就永遠取不到。實測 5 題中有 4 題，
        # 這道去重剛好殺掉了唯一含規格數值的那一塊（例如頁 276 的
        # 「1.1 ±0.3 g/m3」）—— 檢索本來找得到，卻在組池階段被丟掉。
        page_key = (doc.id, chunk.page)
        if chunk.page is not None:
            if page_counts.get(page_key, 0) >= _MAX_CHUNKS_PER_PAGE:
                continue
            page_counts[page_key] = page_counts.get(page_key, 0) + 1
        pool.append((chunk, vscore if vscore is not None else 0.0))
        if len(pool) >= pool_size:
            break

    if not rerank_on or len(pool) <= 1:
        return pool[:top_k]
    return rerank.rerank(query, pool, top_k)
