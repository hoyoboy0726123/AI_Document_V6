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

import logging
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
logger = logging.getLogger(__name__)

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


_TAIL_BOUNDARY_CHARS = "。．！？!?;；\n"


def _trim_partial_tail(text: str, max_cut: int = 150) -> str:
    """截斷點落在句中時，收斂到最後一個完整句尾。

    展開文字按字元上限硬切會產生「Step 1. Conduct a fixture mo」這種殘句，
    而模型會忠實照抄殘句進答案（實測 514.8 流程題的表格 cell 就是這樣收尾）。
    最多只往回退 max_cut 字：一個殘句的長度足矣。先前用「最後 30%」當範圍，
    1750 字塊等於允許砍 525 字 —— 實測 v24/v30 的正解數值就在被砍掉的殘段裡，
    數值落地率掉了 0.1。殺殘句不值得賠掉數值。
    """
    # 結尾是句點也視為完整（無論真句尾或小數點，都不值得再往回砍）
    if not text or text[-1] in _TAIL_BOUNDARY_CHARS or text[-1] == ".":
        return text
    floor = max(0, len(text) - max_cut)
    # 從倒數第二個字元開始：句點分支要看 text[i+1]，從最後一個開始會越界
    #（實測 IndexError 讓 context 組裝整個炸掉，評測三題直接「執行失敗」）
    for i in range(len(text) - 2, floor, -1):
        ch = text[i]
        if ch in _TAIL_BOUNDARY_CHARS:
            return text[: i + 1]
        # 英文句號要求後接空白，避免把 514.8 這種小數點當句尾
        if ch == "." and text[i + 1] in " \t":
            return text[: i + 1]
    return text


def expand_chunk_text(db: Session, chunk, *, radius: int, max_chars: int) -> str:
    """小找大：命中塊「完整保留」+ 同文件前後 radius 塊只填剩餘預算，合併去重。

    搜尋精度用小塊（向量比對的是命中塊），但餵 LLM 的是補上鄰塊的大塊，避免句子/數值
    被切塊邊界截斷。命中塊完整保留是關鍵——否則前一塊會把命中塊擠掉、害表格被截尾
    （例如查 101.78 卻只看到表格前兩列）。
    """
    base = chunk.text or ""
    if radius <= 0 or len(base) >= max_chars:
        return _trim_partial_tail(base[:max_chars]) if len(base) > max_chars else base
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
    cut = False  # 尾端是否發生過硬切（切齊上限時長度不會超過 max_chars，比不出來）
    rem = max_chars - len(result)
    if rem > 0 and nxt:
        cut = len(nxt) > rem
        result = _join_dedup(result, nxt[:rem])
    rem = max_chars - len(result)
    if rem > 0 and prv:
        result = _join_dedup(prv[-rem:], result)
    if len(result) > max_chars:
        result, cut = result[:max_chars], True
    return _trim_partial_tail(result) if cut else result


def budget_for(filtered, *, sample_chunks: int = 6) -> int:
    """用整組候選段落估一次 context 預算（字元）。呼叫端算一次、全程沿用。"""
    sample = "\n".join((c.text or "") for c, _ in (filtered or [])[:sample_chunks])
    return ai.effective_rag_budget(sample)


def context_text_budgeted(db: Session, chunk, ctx_used: int,
                          budget: Optional[int] = None) -> str:
    """在「總 context 預算」內做鄰塊擴展。

    關鍵：預算用完時必須「截斷到剩餘額度」(或回空)，不能回傳整塊——否則多塊加總會
    超過 num_ctx，導致模型(如 gemma)吃到溢出的 prompt 而吐空，最後落到「暫無足夠資訊」。
    """
    base = chunk.text or ""
    # budget 必須由呼叫端「用整組候選估一次」後傳進來。若每塊各自估，密度低的
    # 那一塊會給出比較寬鬆的額度，總量隨最後一塊是誰而浮動（實測同一題在
    # 8374~10262 之間跳動）。留 None 只為向後相容。
    total_budget = budget if budget is not None else ai.effective_rag_budget(chunk.text or "")
    remaining = total_budget - ctx_used
    if remaining <= 0:
        return ""  # 預算已用完：不再塞，避免 prompt 溢出 num_ctx
    radius = getattr(settings, "RAG_NEIGHBOR_RADIUS", 1)
    per_cap = getattr(settings, "RAG_EXPAND_MAX_CHARS", 2000)
    if radius <= 0 or remaining <= len(base):
        # 截斷到剩餘額度（原本回傳整塊 → 溢出 bug），並收斂到句尾
        return _trim_partial_tail(base[:remaining]) if len(base) > remaining else base
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


# 程序覆蓋補撈的觸發條件：問的是「怎麼做／流程／程序」且點名了方法號。
# 沒點名方法號就不觸發（無從界定範圍），維持現狀、不產生新誤擋。
_PROC_INTENT_RE = re.compile(
    r"程序|流程|步驟|怎麼進行|如何進行|怎麼做|怎麼執行|"
    r"procedures?|how\s+(?:to\s+)?(?:conduct|perform|run|carry)",
    re.I,
)
_METHOD_NUM_IN_QUERY_RE = re.compile(r"\b(\d{3}\.\d{1,2})\b")
_FILL_MAX_CHUNKS = 4


def _fill_procedure_coverage(db: Session, query: str, selected, *,
                             document_id: Optional[str] = None):
    """程序類問題的 enumerate-then-fill。

    先用 KG+方法內文掃出該方法的 Procedure I…N 完整清單（list_procedures，
    確定性、零 LLM 成本），再檢查已選塊的覆蓋：哪個 Procedure 連提都沒提 →
    把它標題所在的那一塊直接補進來（塊 id 是掃描時記下的，不需再檢索）。
    實測「Method 514.8 的測試流程」原本只覆蓋 I–III，Procedure IV 的內文塊
    排在 top_k 外，答案只能寫「未完整提供內容」。
    """
    if not selected or not _PROC_INTENT_RE.search(query or ""):
        return selected
    if not _METHOD_NUM_IN_QUERY_RE.search(query or ""):
        return selected
    from . import agent_tools  # 延遲載入避免循環匯入（agent_tools 匯入 retrieval）
    pr = agent_tools.run_tool(db, "list_procedures", {"name": query})
    procs = (pr or {}).get("procedures") if isinstance(pr, dict) else None
    if not procs:
        return selected
    joined = "\n".join((c.text or "") for c, _s in selected)
    have_ids = {c.id for c, _s in selected}
    added = 0
    for p in procs:
        if added >= _FILL_MAX_CHUNKS:
            break
        num = p.get("procedure")
        cid = p.get("chunk_id")
        if not num or not cid or cid in have_ids:
            continue
        # \b 擋不住羅馬數字前綴（I 會命中 II），用否定 lookahead
        if re.search(rf"Procedure\s+{num}(?![IVX])", joined, re.I):
            continue
        chunk = db.query(models.DocumentChunk).filter(models.DocumentChunk.id == cid).first()
        if not chunk or (document_id and chunk.document_id != document_id):
            continue
        selected = list(selected) + [(chunk, None)]
        have_ids.add(cid)
        joined += "\n" + (chunk.text or "")
        added += 1
        logger.info("程序覆蓋補撈：Procedure %s（頁 %s）補進候選", num, chunk.page)
    return selected


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
        # keyword-only 命中保留 None 分數：它不是「相似度 0」，是「沒有向量分數」。
        # 前端據此顯示「關鍵字命中」而非誤導性的 0.000；rerank 據此套 CE 門檻。
        pool.append((chunk, vscore))
        if len(pool) >= pool_size:
            break

    # 向量前 N 名保證進 rerank 池（只保證「被 CE 看到」，不保證排名）。
    #
    # RRF 的病灶：雙訊號候選哪怕兩邊都排第 50（2/110≈0.018），分數仍高於
    # 純向量第 1 名（1/62≈0.016）。中翻英的檢索詞又廣（salt/test/temperature
    # 到處命中），雙訊號候選輕易塞滿 12 格 —— 實測 v02 的正解是向量第 1 名
    # （0.676、CE 0.998），卻連池都進不去。翻譯詞的細微漂移讓這類題在
    # 「剛好擠進／剛好擠出」之間翻面，這裡直接把向量最強訊號釘進池底。
    _VEC_TOP_GUARANTEE = 3
    if rerank_on:
        pool_ids = {c.faiss_id for c, _ in pool}
        vec_top = sorted((x for x in fused if x[1] is not None),
                         key=lambda x: -x[1])[:_VEC_TOP_GUARANTEE]
        for rank_i, (fid, vscore) in enumerate(vec_top, 1):
            if fid in pool_ids:
                continue
            ch = chunk_map.get(fid)
            if not ch or _is_unreadable(ch.text):
                continue
            doc = ch.document
            if document_id and doc.id != document_id:
                continue
            if classification_id and doc.classification_id != classification_id:
                continue
            if project_id and not _project_matches(doc.metadata_data or {}, project_id):
                continue
            if folder_ids and doc.folder_id not in folder_ids:
                continue
            pool.append((ch, vscore))
            pool_ids.add(fid)
            logger.info("向量第 %d 名（%.3f）被 RRF 擠出池外，保底放回 rerank 池", rank_i, vscore)

    if not rerank_on or len(pool) <= 1:
        selected = pool[:top_k]
    else:
        kwonly_idx = {i for i, (_c, s) in enumerate(pool) if s is None}
        selected = rerank.rerank(query, pool, top_k, kwonly=kwonly_idx)

    # 程序類問題的覆蓋補撈（enumerate-then-fill）：檢索按整體相關性排序，
    # 不保證每個 Procedure 都有內文塊 → 「Procedure IV - 未完整提供內容」。
    try:
        selected = _fill_procedure_coverage(db, query, selected, document_id=document_id)
    except Exception as e:  # noqa: BLE001 — 補撈失敗不可拖垮主檢索
        logger.warning("procedure coverage fill failed: %s", e)
    return selected
