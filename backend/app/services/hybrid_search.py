"""混合檢索（Hybrid Retrieval）：向量 + BM25 關鍵字全文，用 RRF 融合。

動機：純向量檢索對「分散、靠關鍵字才找得到」的子項目 recall 很差
（例：查 "pressure test spec" 時，12.5 Prescale Film Pressure Test 的向量相似度
反而排在修訂紀錄頁之後）。BM25 關鍵字檢索能把含關鍵字的段落排到前面，
兩路結果用 Reciprocal Rank Fusion (RRF) 融合，取雙方互補的優點。

設計重點：
- FTS5 + trigram tokenizer：trigram 對 CJK 友善（≥3 字可命中），不需外部斷詞器。
- FTS 表存在「同一個 SQLite 檔」，rowid = faiss_id，與向量結果用同一把 key 融合。
- 用獨立 sqlite3 連線操作 FTS，不干擾 ORM 的 transaction。
- 索引「列數不符就重建」（涵蓋 ingest / 刪除 / 重抽），免去在 ingestion 各處埋同步點。
- 非 SQLite（如 Postgres）自動退回純向量，保留兩種部署模式。
"""

from __future__ import annotations

import concurrent.futures
from concurrent.futures import TimeoutError as FuturesTimeout
import logging
import re
import sqlite3
import threading
from typing import Dict, List, Optional, Set, Tuple

from sqlalchemy.orm import Session

from ..core.config import settings
from . import vector_store

logger = logging.getLogger(__name__)

_FTS_TABLE = "chunk_fts"
_BUILD_LOCK = threading.Lock()

# trigram tokenizer 需要 token 長度 ≥ 3 才能命中
_EN_TOKEN_RE = re.compile(r"[A-Za-z0-9]{3,}")
_CJK_RUN_RE = re.compile(r"[一-鿿]{3,}")
# 小數/帶點數字整體保留（如 101.78、34.8500），讓「查表格某數值」能精準命中該列
# （trigram 對 "101.78" 會命中 "101.7800"）；否則 [A-Za-z0-9]{3,} 會把它拆成 "101" 等無鑑別力的詞。
_DECIMAL_RE = re.compile(r"\d+(?:\.\d+)+")


def _sqlite_path(db: Session) -> Optional[str]:
    """從 SQLAlchemy session 取得 SQLite 檔路徑；非 SQLite 回 None。"""
    try:
        bind = db.get_bind()
        if bind.dialect.name != "sqlite":
            return None
        path = bind.url.database
        if not path or path == ":memory:":
            return None
        return path
    except Exception:
        return None


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _ensure_index(conn: sqlite3.Connection, *, force: bool = False) -> bool:
    """確保 FTS 表存在且與 document_chunks 同步；回傳是否可用。"""
    cur = conn.cursor()
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (_FTS_TABLE,)
    )
    exists = cur.fetchone() is not None
    if not exists:
        cur.execute(
            f"CREATE VIRTUAL TABLE {_FTS_TABLE} USING fts5(text, tokenize = 'trigram')"
        )

    src_n = cur.execute("SELECT count(*) FROM document_chunks").fetchone()[0]
    fts_n = (
        cur.execute(f"SELECT count(*) FROM {_FTS_TABLE}").fetchone()[0] if exists else 0
    )
    if force or not exists or src_n != fts_n:
        cur.execute(f"DELETE FROM {_FTS_TABLE}")
        cur.execute(
            f"INSERT INTO {_FTS_TABLE}(rowid, text) "
            f"SELECT faiss_id, text FROM document_chunks WHERE faiss_id IS NOT NULL"
        )
        conn.commit()
        logger.info("hybrid_search: FTS index rebuilt (%d rows)", src_n)
    return True


def _build_match(query: str) -> Optional[str]:
    """把使用者問題轉成 FTS5 MATCH 字串（各 term 以 OR 連接，提高 recall）。"""
    if not query:
        return None
    terms: List[str] = []
    # 帶小數的數字整體保留（高鑑別力，能獨指某一列）
    terms.extend(_DECIMAL_RE.findall(query))
    for tok in _EN_TOKEN_RE.findall(query):
        terms.append(tok.lower())
    terms.extend(_CJK_RUN_RE.findall(query))
    seen: List[str] = []
    for t in terms:
        if t not in seen:
            seen.append(t)
    if not seen:
        return None
    return " OR ".join('"%s"' % t.replace('"', "") for t in seen)


_HAS_CJK_RE = re.compile(r"[一-鿿]")
_EN_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-]*")
_TRANSLATE_CACHE: Dict[str, str] = {}
_TRANSLATE_CACHE_MAX = 512
_TRANSLATE_LOCK = threading.Lock()
# 共用 executor：逾時後不等待該執行緒收尾（見 keyword_search 內註解）。
# worker 數刻意小，避免翻譯卡住時無上限累積執行緒。
_TRANSLATE_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="kwtranslate"
)

_TRANSLATE_PROMPT = (
    "把下面的中文技術問題，轉成 2-4 個用於全文檢索的英文關鍵詞。\n"
    "只輸出關鍵詞，用空白分隔，不要解釋、不要標點、不要編號。\n\n"
    "問題：{q}\n英文關鍵詞："
)


def _translate_for_keyword(query: str) -> str:
    """把中文查詢轉成英文檢索詞（含快取）。任何失敗都回空字串，由呼叫端當作沒發生。"""
    key = query.strip()
    with _TRANSLATE_LOCK:
        hit = _TRANSLATE_CACHE.get(key)
    if hit is not None:
        return hit

    from .llm_provider import get_llm_provider

    provider = get_llm_provider()
    raw = provider.chat(
        [{"role": "user", "content": _TRANSLATE_PROMPT.format(q=key)}],
        options={"temperature": 0.0, "num_ctx": 2048},
    )
    terms = " ".join(_EN_WORD_RE.findall(raw or ""))[:120]

    with _TRANSLATE_LOCK:
        if len(_TRANSLATE_CACHE) >= _TRANSLATE_CACHE_MAX:
            _TRANSLATE_CACHE.clear()  # 簡單汰換：滿了就清空，避免無上限成長
        _TRANSLATE_CACHE[key] = terms
    return terms


def _fts_query(path: str, match: str, top_k: int) -> List[Tuple[int, float]]:
    with _BUILD_LOCK:
        conn = _connect(path)
        try:
            _ensure_index(conn)
            rows = conn.execute(
                f"SELECT rowid, bm25({_FTS_TABLE}) AS s "
                f"FROM {_FTS_TABLE} WHERE {_FTS_TABLE} MATCH ? "
                f"ORDER BY s LIMIT ?",
                (match, top_k),
            ).fetchall()
        finally:
            conn.close()
    return [(int(r[0]), float(r[1])) for r in rows]


def keyword_search(db: Session, query: str, top_k: int) -> List[Tuple[int, float]]:
    """BM25 關鍵字檢索；回傳 [(faiss_id, bm25_score)]，bm25 越小越相關。"""
    if not getattr(settings, "RAG_HYBRID_SEARCH", True):
        return []
    path = _sqlite_path(db)
    if not path:
        return []
    match = _build_match(query)
    if not match:
        return []
    try:
        rows = _fts_query(path, match, top_k)
    except Exception as e:  # FTS 任何問題都退回純向量，絕不讓檢索整個壞掉
        logger.warning("hybrid_search keyword_search failed, falling back: %s", e)
        return []

    if rows:
        return rows

    # 撈到 0 筆且查詢含中文 → 語料很可能是英文（MIL-STD 等），中文整句被當成單一片語
    # 而永遠不命中。此時才付翻譯成本，轉成英文檢索詞重試一次。
    # 只在「本來就已經失效」的情況觸發，因此不會動到原本正常的查詢。
    if not getattr(settings, "RAG_QUERY_TRANSLATE_FALLBACK", True):
        return rows
    if not _HAS_CJK_RE.search(query):
        return rows

    try:
        # provider.chat 沒有 timeout 參數，而 OLLAMA_TIMEOUT 預設可達數百秒；
        # 這是「救援路徑」，不能讓它拖住整個查詢，因此用執行緒強制設上限。
        # 注意：不可用 `with ThreadPoolExecutor(...)`，離開 with 會 shutdown(wait=True)
        # 阻塞等待該執行緒跑完，逾時形同虛設（實測仍等滿 30s）。改用共用的 executor，
        # 逾時就放生那個執行緒，讓查詢先走。
        limit = int(getattr(settings, "RAG_QUERY_TRANSLATE_TIMEOUT", 8) or 8)
        terms = _TRANSLATE_POOL.submit(_translate_for_keyword, query).result(timeout=limit)
        if not terms:
            return rows
        retry_match = _build_match(terms)
        if not retry_match:
            return rows
        rows = _fts_query(path, retry_match, top_k)
        logger.info(
            "hybrid_search: 中文查詢 BM25 0 命中，改用英文檢索詞「%s」重試 → %d 筆",
            terms, len(rows),
        )
        return rows
    except FuturesTimeout:
        # 逾時是最常見的失敗，而 TimeoutError 的字串是空的 —— 原本只印 %s 會變成
        # 「translate fallback failed, keeping vector-only: 」什麼都看不出來。
        # 這條救援路徑失敗代表中文查詢完全沒有關鍵字訊號，值得明確記錄。
        logger.warning(
            "hybrid_search 中翻英救援逾時（%.0fs，模型忙碌時常見）→ 本次查詢僅靠向量檢索",
            limit,
        )
        return []
    except Exception as e:  # 其他失敗一律當作沒發生，維持原本的純向量行為
        logger.warning("hybrid_search translate fallback failed (%s): %s",
                       type(e).__name__, e)
        return []


def fuse(
    db: Session,
    query: str,
    embedding: List[float],
    candidate_k: int,
) -> Tuple[List[Tuple[int, Optional[float]]], Set[int]]:
    """向量 + 關鍵字 RRF 融合。

    回傳：
      - ordered: [(faiss_id, vector_score 或 None)]，依 RRF 分數由高到低
      - kw_hits: 出現在關鍵字結果中的 faiss_id 集合（供呼叫端放行 keyword-only 命中）
    """
    rrf_k = getattr(settings, "RAG_RRF_K", 60)
    vec = vector_store.search(embedding, candidate_k)
    kw = keyword_search(db, query, candidate_k)

    vec_rank: Dict[int, int] = {fid: i for i, (fid, _) in enumerate(vec)}
    kw_rank: Dict[int, int] = {fid: i for i, (fid, _) in enumerate(kw)}
    vec_score: Dict[int, float] = {fid: s for fid, s in vec}

    if not kw_rank:
        # 沒有關鍵字結果（非 SQLite / 無有效 term / FTS 失敗）→ 等同原本純向量
        return [(fid, vec_score.get(fid)) for fid, _ in vec], set()

    fids = set(vec_rank) | set(kw_rank)

    def _rrf(fid: int) -> float:
        s = 0.0
        if fid in vec_rank:
            s += 1.0 / (rrf_k + vec_rank[fid] + 1)
        if fid in kw_rank:
            s += 1.0 / (rrf_k + kw_rank[fid] + 1)
        return s

    # 平局要有確定性的次序。只出現在單一名單的候選，RRF 分數必定是
    # 1/(k+i+1)，因此大量精確平手（實測 90 個候選只有 57 個相異分數、
    # 66 個處於平手）。sorted 雖是穩定排序，但輸入是 set 的迭代順序 ——
    # 由 int hash bucket 決定，與相關性無關，同一題兩次執行可能不同名次。
    # 平手時改用「兩張名單中較前面的那個名次」決勝。
    INF = 10 ** 9

    def _tie_break(fid: int) -> int:
        return min(vec_rank.get(fid, INF), kw_rank.get(fid, INF))

    ordered = sorted(fids, key=lambda f: (-_rrf(f), _tie_break(f)))
    return [(fid, vec_score.get(fid)) for fid in ordered], set(kw_rank)
