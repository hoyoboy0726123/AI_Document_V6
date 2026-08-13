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
_FTS_META = "chunk_fts_meta"
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

    # 同步判斷改用「內容指紋」而非只比列數。
    #
    # 只比列數有兩個問題：
    #   1. 重新抽取一份文件是「刪 N 筆再加 N 筆」，列數不變 → FTS 永遠比對舊
    #      文字，並回傳已不存在的 faiss_id（在 pool 迴圈被靜默丟棄，白白吃掉
    #      候選名額）。今天重抽了 40 次，這個風險是實際的。
    #   2. 來源計數沒有加 WHERE faiss_id IS NOT NULL，但 INSERT 有 —— 只要出現
    #      一筆 NULL，兩數永遠不相等，於是**每一次查詢都會全量重建**（實測
    #      7558 列要 11.4 秒，而且握著鎖）。
    #
    # 指紋取 (列數, 文字總長, 最後更新時間)：內容換掉但列數相同時總長極難剛好
    # 一致，加上 updated_at 幾乎不可能碰撞。成本是一次聚合查詢（毫秒級）。
    fingerprint = str(cur.execute(
        "SELECT count(*), COALESCE(SUM(LENGTH(text)),0), COALESCE(MAX(updated_at),'') "
        "FROM document_chunks WHERE faiss_id IS NOT NULL"
    ).fetchone())

    cur.execute(f"CREATE TABLE IF NOT EXISTS {_FTS_META} (k TEXT PRIMARY KEY, v TEXT)")
    prev = cur.execute(f"SELECT v FROM {_FTS_META} WHERE k='fingerprint'").fetchone()
    stale = (prev is None) or (prev[0] != fingerprint)

    if force or not exists or stale:
        cur.execute(f"DELETE FROM {_FTS_TABLE}")
        cur.execute(
            f"INSERT INTO {_FTS_TABLE}(rowid, text) "
            f"SELECT faiss_id, text FROM document_chunks WHERE faiss_id IS NOT NULL"
        )
        cur.execute(f"INSERT OR REPLACE INTO {_FTS_META}(k, v) VALUES('fingerprint', ?)",
                    (fingerprint,))
        conn.commit()
        n = cur.execute(f"SELECT count(*) FROM {_FTS_TABLE}").fetchone()[0]
        logger.info("hybrid_search: FTS index rebuilt (%d rows)", n)
    return True


# 在 MIL-STD 語料中沒有鑑別力的詞。
#
# 關鍵是 trigram tokenizer 做的是**子字串比對**：`"mil"` 會命中每一個含
# "mil" 的位置，而 MIL-STD-810H 的頁首每頁都有。實測「MIL-STD-810H 沙塵
# 測試濃度」被拆成 `"mil" OR "std" OR "810h" OR "沙塵測試濃度"`，BM25 回傳
# 50 筆分數幾乎一致（-4.49 ~ -4.36）的結果 —— 那是 50 筆隨機噪音，而 RRF
# 還會獎勵它們（兩張名單都上榜的噪音佔滿了 rerank 的候選名額）。
#
# 規範編號本身有鑑別力，所以整串保留（見下方 _SPEC_TOKEN_RE），只濾掉
# 拆碎後的無意義片段與英文虛詞。
# 刻意只列「可證明是錯的」那些：mil / std / hdbk / dtl / prf 是規範編號被
# [A-Za-z0-9]{3,} 拆碎後的殘片，本身不是使用者輸入的詞。
#
# 一度連 test / method / standard / requirement 這些也濾掉，但那是「憑直覺
# 認為沒有鑑別力」——golden set 量測顯示那一版 recall 反而少 1 題。它們是
# 使用者真正輸入的內容詞，濾掉會連帶砍掉訊號。沒有量測支持就不要擴大。
_BM25_STOPWORDS = {"mil", "std", "hdbk", "dtl", "prf"}
# 規範編號整串當一個 term，不要被 [A-Za-z0-9]{3,} 拆成 mil / std / 810h
_SPEC_TOKEN_RE = re.compile(
    r"MIL-(?:STD|HDBK|DTL|PRF)-\d+[A-Z]?(?:-\d+)?|AR\s?\d+-\d+|ASTM\s?[A-Z]?\d+",
    re.I,
)


def _build_match(query: str) -> Optional[str]:
    """把使用者問題轉成 FTS5 MATCH 字串（各 term 以 OR 連接，提高 recall）。"""
    if not query:
        return None
    terms: List[str] = []
    # 帶小數的數字整體保留（高鑑別力，能獨指某一列）
    terms.extend(_DECIMAL_RE.findall(query))
    # 規範編號整串保留，並從後續 token 化的來源字串中移除
    spec_ids = _SPEC_TOKEN_RE.findall(query)
    terms.extend(s.upper() for s in spec_ids)
    # 小數已整串保留，其整數部分（101.78 的 101）不要再當一個 term ——
    # 純數字片段在 trigram 下鑑別力極低，只會稀釋 BM25 排序。
    rest = _DECIMAL_RE.sub(" ", _SPEC_TOKEN_RE.sub(" ", query))
    for tok in _EN_TOKEN_RE.findall(rest):
        low = tok.lower()
        if low not in _BM25_STOPWORDS:
            terms.append(low)
    terms.extend(_CJK_RUN_RE.findall(rest))
    seen: List[str] = []
    for t in terms:
        if t not in seen:
            seen.append(t)
    if not seen:
        return None
    return " OR ".join('"%s"' % t.replace('"', "") for t in seen)


# ── 短縮寫的補救檢索 ────────────────────────────────────────────────
# trigram tokenizer 需要 token ≥ 3 字元，因此「ER」「PR」「EC」這類兩字母縮寫
# 在 BM25 裡是**完全搜不到的**：實測 `MATCH "ER"` 回 0 塊，而「ER PR是什麼?」
# 經 _build_match 只剩 '"是什麼"'（同樣 0 塊）。縮寫查詢因此只剩向量一條路。
#
# 而向量對這種查詢特別不可靠：定義往往被埋在一個主要在講別的事的大塊裡。
# 實測教材的 ER/PR 定義位在 1698 字元塊的第 444 字元處，整塊語意被前面的
# 料件代號表主導，相似度 0.373 —— 反而**低於** MIL-STD-461G 描述「兩個字母
# 加三位數字代號」的段落（0.38-0.43）。兩條路都失效，答案就撈不到。
#
# 補一條 GLOB 精確比對。字元類充當詞界，整段在 SQLite C 層掃描，實測 6.5k 塊
# 約 0.2-0.6 秒，且只在查詢真的含兩字母縮寫時才觸發。
_SHORT_ABBREV_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{2}(?![A-Za-z0-9])")
# 命中太多代表這個縮寫沒有鑑別力（例如出現在每頁頁首），放棄它而不是灌爆候選池
_ABBREV_MAX_HITS = 30
_ABBREV_MAX_TOKENS = 3  # 掃描成本上限
# keyword_search 回傳值的名次才有意義（fuse 只看 enumerate 的位置），
# 分數本身不參與計算；用一個明顯不是 bm25 的哨兵值，方便日誌辨識。
_ABBREV_SCORE = -99.0


def _abbrev_query(path: str, query: str) -> List[int]:
    """對查詢中的兩字母縮寫做 GLOB 精確比對，回傳 faiss_id。任何失敗都回空清單。"""
    toks: List[str] = []
    for t in _SHORT_ABBREV_RE.findall(query or ""):
        if t not in toks:
            toks.append(t)
    if not toks:
        return []
    try:
        conn = _connect(path)
    except Exception as e:
        logger.warning("縮寫補救檢索取不到連線：%s", e)
        return []
    per_tok: List[List[int]] = []
    try:
        with _BUILD_LOCK:
            _ensure_index(conn)
        for tok in toks[:_ABBREV_MAX_TOKENS]:
            try:
                rows = conn.execute(
                    f"SELECT rowid FROM {_FTS_TABLE} WHERE text GLOB ? LIMIT ?",
                    (f"*[^A-Za-z0-9]{tok}[^A-Za-z0-9]*", _ABBREV_MAX_HITS + 1),
                ).fetchall()
            except Exception as e:
                logger.warning("縮寫「%s」GLOB 檢索失敗：%s", tok, e)
                continue
            if len(rows) > _ABBREV_MAX_HITS:
                logger.info("縮寫「%s」命中超過 %d 塊，無鑑別力，略過", tok, _ABBREV_MAX_HITS)
                continue
            per_tok.append([fid for (fid,) in rows])
    finally:
        conn.close()
    if not per_tok:
        return []
    # 多個縮寫時優先取「同時含全部縮寫」的塊：使用者一次問 ER 和 PR，兩者都在
    # 的塊幾乎必是定義處（實測交集恰為 1 塊）；單一縮寫的字面命中則常是噪音
    # （MIL-HDBK-454 的「Army - ER」管理單位代碼），有交集時不讓它們進候選、
    # 佔掉 rerank 名額還以相似度 0.000 出現在引用列表裡。
    out: List[int] = []
    if len(per_tok) > 1:
        inter = set(per_tok[0]).intersection(*per_tok[1:])
        if inter:
            out = [fid for fid in per_tok[0] if fid in inter]
            logger.info("縮寫補救檢索：%s 交集 → %d 塊",
                        "/".join(toks[:_ABBREV_MAX_TOKENS]), len(out))
            return out
    for fids in per_tok:
        for fid in fids:
            if fid not in out:
                out.append(fid)
    if out:
        logger.info("縮寫補救檢索：%s → %d 塊", "/".join(toks[:_ABBREV_MAX_TOKENS]), len(out))
    return out


def _merge_abbrev(abbrev: List[int], rows: List[Tuple[int, float]],
                  top_k: int) -> List[Tuple[int, float]]:
    """把縮寫命中排到 BM25 名單前面。

    放前面是因為這是使用者明確打出來的稀有字面詞的精確命中（實測 ER 全語料
    只有 9 塊），精確度遠高於 trigram 的子字串比對。數量已由 _ABBREV_MAX_HITS
    封頂，不會把 BM25 名單整個擠掉。
    """
    if not abbrev:
        return rows
    seen = set(abbrev)
    merged = [(fid, _ABBREV_SCORE) for fid in abbrev]
    merged.extend((fid, s) for fid, s in rows if fid not in seen)
    return merged[:top_k]


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
    # 翻譯是輔助任務，不需要主 LLM 的品質；主 LLM 較慢（gemma4:12b）時
    # 用 RAG_TRANSLATE_MODEL 指定的快模型，避免 15 秒逾時丟失 BM25 訊號。
    _model = getattr(settings, "RAG_TRANSLATE_MODEL", None) or None
    raw = provider.chat(
        [{"role": "user", "content": _TRANSLATE_PROMPT.format(q=key)}],
        model=_model,
        options={"temperature": 0.0, "num_ctx": 2048},
    )
    terms = " ".join(_EN_WORD_RE.findall(raw or ""))[:120]

    with _TRANSLATE_LOCK:
        if len(_TRANSLATE_CACHE) >= _TRANSLATE_CACHE_MAX:
            _TRANSLATE_CACHE.clear()  # 簡單汰換：滿了就清空，避免無上限成長
        _TRANSLATE_CACHE[key] = terms
    return terms


def _fts_query(path: str, match: str, top_k: int) -> List[Tuple[int, float]]:
    # 鎖只包「確保索引存在／重建」，不包查詢本身。
    #
    # 原本整段都在 _BUILD_LOCK 內，所有關鍵字查詢因而完全序列化（實測 8 個
    # 並行查詢與 8 個序列查詢耗時相同，加速比 0.96x）。平常每次 27ms 看不出來，
    # 但一旦觸發全量重建（11.4 秒），所有並行請求全部排隊等它。
    conn = _connect(path)
    try:
        with _BUILD_LOCK:
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
        logger.info("keyword_search 略過：RAG_HYBRID_SEARCH=False")
        return []
    path = _sqlite_path(db)
    if not path:
        logger.warning("keyword_search 略過：取不到 SQLite 路徑（非 SQLite 或 session 無 bind）")
        return []
    # 兩字母縮寫 trigram 索引不到，先另走 GLOB 補一路（見 _abbrev_query 註解）
    abbrev = _abbrev_query(path, query)

    match = _build_match(query)
    if not match:
        if abbrev:
            return _merge_abbrev(abbrev, [], top_k)
        logger.info("keyword_search 略過：查詢無可用 term「%s」", query[:40])
        return []
    try:
        rows = _fts_query(path, match, top_k)
    except Exception as e:  # FTS 任何問題都退回純向量，絕不讓檢索整個壞掉
        logger.warning("hybrid_search keyword_search failed, falling back: %s", e)
        return _merge_abbrev(abbrev, [], top_k)

    if rows or abbrev:
        # 有縮寫命中就不必再付中翻英的錢（最久 8 秒）：縮寫是字面精確命中，
        # 訊號品質比翻譯出來的近義詞高。
        return _merge_abbrev(abbrev, rows, top_k)

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
        # 這兩條原本是靜默 return —— 中文查詢因此完全失去 BM25 訊號，
        # 而日誌上一個字都沒有。實測伺服器端就是走了這裡：同一題在腳本裡
        # CE 0.997 答得出來，走 HTTP 卻回「查無相關資料」，查了很久才發現
        # 差別在這條救援路徑靜默失敗。救援路徑失敗一定要留下痕跡。
        if not terms:
            logger.warning("中文查詢 BM25 0 命中，翻譯回退取不到英文詞（空回應）→ 僅剩向量檢索：%s", query[:40])
            return rows
        retry_match = _build_match(terms)
        if not retry_match:
            logger.warning("中文查詢 BM25 0 命中，翻譯結果無法組成 FTS 查詢「%s」→ 僅剩向量檢索", terms[:60])
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
