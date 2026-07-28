"""查詢翻譯 → 答案層 A/B。只讀，不改系統。

檢索層已證實：中文查詢時 BM25 命中 0 筆，加英文檢索詞後 recall@5 由 0.567 升到 0.667。
這裡驗證關鍵問題：recall 提升有沒有真的轉化成更好的答案。

對照組 A：現行行為（中文問題 → 向量 + BM25(0 筆) → top-5 → 生成）
實驗組 B：中文問題 → LLM 轉英文檢索詞 → 向量 + BM25 → RRF → top-5 → 生成

沿用 adjacent_expand_eval2 修正過的落地率指標；每組固定 seed 跑 3 次。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
import time

import numpy as np
import requests

sys.path.insert(0, "scripts")
from adjacent_expand_eval2 import CITE_RE, HEDGES, grounded  # noqa: E402
from query_translate_ab import (  # noqa: E402
    CASES, LLM_MODEL, OLLAMA, build_match, embed, rrf_fuse, translate,
)

ANSWER_PROMPT = """你是專業的軍規測試標準查詢助理。請「只依據」以下參考資料回答問題，
不得加入資料中沒有的內容。若資料不足以回答，請明說缺什麼。用繁體中文回答。

參考資料：
{ctx}

問題：{q}

回答："""


def ask(prompt: str, seed: int) -> tuple[str, float]:
    t0 = time.perf_counter()
    r = requests.post(f"{OLLAMA}/api/chat", json={
        "model": LLM_MODEL, "messages": [{"role": "user", "content": prompt}],
        "think": False, "stream": False,
        "options": {"num_ctx": 16384, "temperature": 0.2, "seed": seed},
    }, timeout=900)
    r.raise_for_status()
    return r.json()["message"]["content"].strip(), time.perf_counter() - t0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="doc_management.db")
    ap.add_argument("--title-like", default="%810H%")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--candidates", type=int, default=30)
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    rows = con.execute(
        """select ch.faiss_id, ch.page, ch.text, ch.embedding
           from document_chunks ch join documents d on d.id = ch.document_id
           where d.title like ? and ch.embedding is not null and ch.faiss_id is not null""",
        (args.title_like,)).fetchall()
    fids = [int(r[0]) for r in rows]
    pages = {int(r[0]): r[1] for r in rows}
    texts = {int(r[0]): r[2] for r in rows}
    V = np.array([json.loads(r[3]) for r in rows], dtype="float32")
    V /= np.linalg.norm(V, axis=1, keepdims=True) + 1e-12
    valid = set(fids)

    print(f"語料 {len(fids)} 塊　{LLM_MODEL}（關思考）　top-{args.k}　每組 {args.runs} 次")
    print(f"題數 {len(CASES)}　共 {len(CASES)*2*args.runs} 次生成\n")

    agg = {"A": [], "B": []}
    detail = []
    trans_times = []

    for qi, (q, term) in enumerate(CASES, 1):
        sims = V @ embed(q)
        vec_ids = [fids[i] for i in np.argsort(-sims)[: args.candidates]]

        t0 = time.perf_counter()
        en = translate(q)
        trans_times.append(time.perf_counter() - t0)

        tops = {}
        for arm, kwq in (("A", q), ("B", f"{q} {en}")):
            m = build_match(kwq)
            kw_ids = []
            if m:
                try:
                    kw_ids = [int(r[0]) for r in con.execute(
                        "SELECT rowid FROM chunk_fts WHERE chunk_fts MATCH ? "
                        "ORDER BY bm25(chunk_fts) LIMIT ?", (m, args.candidates)).fetchall()]
                except Exception:
                    kw_ids = []
            kw_ids = [f for f in kw_ids if f in valid]
            tops[arm] = rrf_fuse(vec_ids, kw_ids, args.k)

        rec: dict = {"q": q, "term": term, "en": en, "runs": {"A": [], "B": []},
                     "same_topk": sorted(tops["A"]) == sorted(tops["B"])}
        for arm in ("A", "B"):
            ctx = "\n\n".join(f"[第{pages[f]}頁] {texts[f]}" for f in tops[arm])
            for run in range(args.runs):
                ans, dt = ask(ANSWER_PROMPT.format(ctx=ctx, q=q), seed=1000 + run)
                g, miss = grounded(ans, ctx)
                rec["runs"][arm].append({
                    "g": g, "missing": miss, "hedge": sum(ans.count(x) for x in HEDGES),
                    "cites": len(CITE_RE.findall(ans)), "sec": dt, "len": len(ans), "ans": ans,
                })
            agg[arm].extend(rec["runs"][arm])
        detail.append(rec)

        def mean(arm, key):
            vals = [r[key] for r in rec["runs"][arm] if r[key] is not None]
            return statistics.mean(vals) if vals else float("nan")

        same = "（top-5 相同）" if rec["same_topk"] else ""
        print(f"Q{qi:2} {q[:20]:20} 落地率 A={mean('A','g'):.2f} B={mean('B','g'):.2f}　"
              f"引用 A={mean('A','cites'):.1f} B={mean('B','cites'):.1f}　"
              f"保留語 A={mean('A','hedge'):.1f} B={mean('B','hedge'):.1f} {same}")

    con.close()

    print("\n" + "=" * 78)
    print(f"彙總（{len(CASES)} 題 × {args.runs} 次）")
    for arm, name in (("A", "對照組(現行)"), ("B", "實驗組(+查詢翻譯)")):
        f = agg[arm]
        gs = [r["g"] for r in f if r["g"] is not None]
        print(f"  {arm} {name}：落地率 {statistics.mean(gs):.3f}±{statistics.pstdev(gs):.3f}　"
              f"引用 {statistics.mean([r['cites'] for r in f]):.2f}　"
              f"保留語 {statistics.mean([r['hedge'] for r in f]):.2f}　"
              f"生成 {statistics.mean([r['sec'] for r in f]):.1f}s　"
              f"{statistics.mean([r['len'] for r in f]):.0f}字")

    win = {"A": 0, "B": 0, "tie": 0}
    for rec in detail:
        ga = statistics.mean([r["g"] for r in rec["runs"]["A"] if r["g"] is not None] or [0])
        gb = statistics.mean([r["g"] for r in rec["runs"]["B"] if r["g"] is not None] or [0])
        win["B" if gb - ga > 0.05 else "A" if ga - gb > 0.05 else "tie"] += 1
    print(f"\n  逐題落地率勝負：B 勝 {win['B']}　A 勝 {win['A']}　平手 {win['tie']}")
    same_n = sum(1 for r in detail if r["same_topk"])
    print(f"  top-5 完全相同的題數：{same_n}/{len(detail)}（相同代表這題檢索沒被改變）")
    print(f"  翻譯額外耗時：平均 {statistics.mean(trans_times):.2f}s／次查詢")

    with open("scripts/translate_answer_detail.json", "w", encoding="utf-8") as fp:
        json.dump(detail, fp, ensure_ascii=False, indent=2)
    print("\n完整答案已存：scripts/translate_answer_detail.json")


if __name__ == "__main__":
    main()
