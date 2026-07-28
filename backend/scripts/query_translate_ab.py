"""中文查詢 → BM25 失效問題的 A/B 驗證。只讀，不改系統。

已證實的缺口：語料是英文 MIL-STD，使用者用中文問，FTS5 trigram 對中文查詢
回傳 0 筆，混合檢索實際退化成純向量。

對照組 A：現行行為（中文問題直接進 BM25）
實驗組 B：先用 LLM 把中文問題轉成英文檢索詞，再進 BM25

指標不靠 LLM 判斷，用可客觀驗證的標準答案：
  標準答案 = 內文含指定英文術語的 chunk
  hit@k    = top-k 裡是否至少命中一個標準答案
  recall@k = top-k 命中的標準答案佔全部標準答案的比例
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import statistics

import numpy as np
import requests

OLLAMA = "http://127.0.0.1:11434"
EMBED_MODEL = "qwen3-embedding:8b"
LLM_MODEL = "qwen3:8b"
RRF_K = 60

# (中文問題, 判定標準答案用的英文術語)
CASES = [
    ("鹽霧測試的鹽溶液濃度要求是什麼", "salt fog"),
    ("淋雨測試的降雨率要求為何", "rainfall rate"),
    ("溫度衝擊測試的轉換時間規定", "temperature shock"),
    ("砂塵測試的風速條件", "blowing dust"),
    ("濕度測試的相對濕度條件", "relative humidity"),
    ("爆炸性大氣測試使用什麼燃料", "explosive atmosphere"),
    ("太陽輻射測試的照度要求", "solar radiation"),
    ("黴菌測試使用哪些菌種", "fungus"),
    ("低壓測試模擬的高度條件", "low pressure"),
    ("加速度測試的程序分類", "acceleration"),
    ("酸性大氣測試的暴露條件", "acidic atmosphere"),
    ("結冰與凍雨測試的厚度要求", "icing"),
]

TRANSLATE_PROMPT = """把下面的中文技術問題，轉成 2-4 個用於全文檢索的英文關鍵詞。
只輸出關鍵詞，用空白分隔，不要解釋、不要標點、不要編號。

問題：{q}
英文關鍵詞："""

# 直接引用系統真正的實作，避免自行複製正則造成實驗與線上行為不一致
# （曾因手抄 _EN_TOKEN_RE 出錯，導致一次完全錯誤的「bug 發現」）
import sys as _sys
_sys.path.insert(0, ".")
from app.services.hybrid_search import _build_match as build_match  # noqa: E402

# 僅用於清理 LLM 翻譯輸出（去掉標點、說明文字），與系統的 token 規則無關
_EN_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-]*")


def embed(text: str) -> np.ndarray:
    r = requests.post(f"{OLLAMA}/api/embed",
                      json={"model": EMBED_MODEL, "input": [text], "truncate": True}, timeout=180)
    r.raise_for_status()
    v = np.array(r.json()["embeddings"][0], dtype="float32")
    return v / (np.linalg.norm(v) + 1e-12)


def translate(q: str) -> str:
    r = requests.post(f"{OLLAMA}/api/chat", json={
        "model": LLM_MODEL, "messages": [{"role": "user", "content": TRANSLATE_PROMPT.format(q=q)}],
        "think": False, "stream": False, "options": {"temperature": 0.0, "num_ctx": 2048},
    }, timeout=300)
    r.raise_for_status()
    out = r.json()["message"]["content"].strip()
    return " ".join(_EN_WORD_RE.findall(out))[:120]


def rrf_fuse(vec_ids: list[int], kw_ids: list[int], k: int) -> list[int]:
    vr = {f: i for i, f in enumerate(vec_ids)}
    kr = {f: i for i, f in enumerate(kw_ids)}
    if not kr:
        return vec_ids[:k]
    ids = set(vr) | set(kr)

    def score(f: int) -> float:
        s = 0.0
        if f in vr:
            s += 1.0 / (RRF_K + vr[f] + 1)
        if f in kr:
            s += 1.0 / (RRF_K + kr[f] + 1)
        return s

    return sorted(ids, key=lambda f: -score(f))[:k]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="doc_management.db")
    ap.add_argument("--title-like", default="%810H%")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--candidates", type=int, default=30)
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    rows = con.execute(
        """select ch.faiss_id, ch.page, ch.text, ch.embedding
           from document_chunks ch join documents d on d.id = ch.document_id
           where d.title like ? and ch.embedding is not null and ch.faiss_id is not null""",
        (args.title_like,)).fetchall()

    fids = [int(r[0]) for r in rows]
    texts = [r[2] for r in rows]
    V = np.array([json.loads(r[3]) for r in rows], dtype="float32")
    V /= np.linalg.norm(V, axis=1, keepdims=True) + 1e-12
    fid2row = {f: i for i, f in enumerate(fids)}

    print(f"語料 {len(fids)} 塊　k={args.k}　候選池={args.candidates}\n")
    res = {"A": {"hit": [], "rec": [], "kw": []}, "B": {"hit": [], "rec": [], "kw": []}}

    for qi, (q, term) in enumerate(CASES, 1):
        gt = {fids[i] for i, t in enumerate(texts) if term.lower() in t.lower()}
        if not gt:
            print(f"Q{qi:2} 略過（語料中找不到「{term}」）")
            continue

        sims = V @ embed(q)
        vec_ids = [fids[i] for i in np.argsort(-sims)[: args.candidates]]
        en = translate(q)

        for arm, kwquery in (("A", q), ("B", f"{q} {en}")):
            m = build_match(kwquery)
            kw_ids = []
            if m:
                try:
                    kw_ids = [int(r[0]) for r in con.execute(
                        "SELECT rowid FROM chunk_fts WHERE chunk_fts MATCH ? "
                        "ORDER BY bm25(chunk_fts) LIMIT ?", (m, args.candidates)).fetchall()]
                except Exception:
                    kw_ids = []
            kw_ids = [f for f in kw_ids if f in fid2row]  # 限定同一份文件
            top = rrf_fuse(vec_ids, kw_ids, args.k)
            hit = 1.0 if gt & set(top) else 0.0
            rec = len(gt & set(top)) / min(len(gt), args.k)
            res[arm]["hit"].append(hit)
            res[arm]["rec"].append(rec)
            res[arm]["kw"].append(len(kw_ids))

        print(f"Q{qi:2} {q[:22]:22} GT={len(gt):3}塊　"
              f"BM25命中 A={res['A']['kw'][-1]:3} B={res['B']['kw'][-1]:3}　"
              f"hit@{args.k} A={res['A']['hit'][-1]:.0f} B={res['B']['hit'][-1]:.0f}　"
              f"recall A={res['A']['rec'][-1]:.2f} B={res['B']['rec'][-1]:.2f}")
        print(f"     翻譯→ {en}")

    con.close()
    print("\n" + "=" * 74)
    for arm, name in (("A", "對照組(中文直接進 BM25)"), ("B", "實驗組(+英文檢索詞)")):
        r = res[arm]
        print(f"  {arm} {name}：hit@{args.k} {statistics.mean(r['hit']):.2f}　"
              f"recall@{args.k} {statistics.mean(r['rec']):.3f}　"
              f"BM25 平均命中 {statistics.mean(r['kw']):.1f} 筆")


if __name__ == "__main__":
    main()
