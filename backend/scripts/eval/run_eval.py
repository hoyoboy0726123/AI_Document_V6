"""RAG 回歸評測：一鍵跑出檢索與生成的品質指標。

為什麼需要這支
--------------
在此之前，每次改動都是「跑一次看結果」判斷好壞。實際紀錄顯示這樣不行：
  * 加了補查後，沙塵測試被答成黴菌測試的培養溫度 —— 但當時的檢查只數
    「答案有幾個數值」，正好給這種錯誤答案滿分
  * 補查預算的修正在單元測試下全綠，實際串流路徑卻完全失效
  * 獨立審查實測顯示「沒有任何單一改動有效」，缺陷互相掩蓋，必須成組套用
    —— 沒有基線就無法判斷一組改動是淨賺還是淨賠

三層指標，各自量不同階段
------------------------
  1. recall@k（融合後、rerank 前）   → 量混合檢索有沒有把正解撈進候選池
  2. hit@5 / MRR（rerank 後）        → 量 rerank 有沒有把正解排上來
  3. 數值落地率 + 幻覺率（生成後）    → 量答案有沒有把數值寫出來、有沒有亂編

前兩層不呼叫 LLM，秒級跑完，適合每次改動都跑。第三層要跑生成，較慢，
用 --with-generation 開啟。

用法：
    python scripts/eval/run_eval.py                      # 只跑檢索層（快）
    python scripts/eval/run_eval.py --with-generation    # 含生成層（慢）
    python scripts/eval/run_eval.py --baseline base.json # 與先前結果比較
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time

sys.path.insert(0, ".")

from app.database import SessionLocal  # noqa: E402
from app.services import ai, retrieval  # noqa: E402

GOLDEN = "scripts/eval/golden.jsonl"


def norm(v: str) -> str:
    s = re.sub(r"\s+", "", v or "").lower()
    return s.replace("º", "°").replace("³", "3").replace("²", "2")


def gold_chunk_ids(con: sqlite3.Connection, golds: list) -> set:
    """由 gold 數值反推「哪些區塊算正解」。

    刻意不寫死頁碼：同一個數值常散落在正文、表格、附錄，指定頁碼會讓評測
    變成考「有沒有撈到我挑的那一頁」。用數值反推，換了分塊策略也不會失效。
    """
    ids = set()
    if not golds:
        return ids
    rows = con.execute("SELECT id, text FROM document_chunks").fetchall()
    normed = [(cid, norm(t)) for cid, t in rows]
    for g in golds:
        gn = norm(g)
        if not gn:
            continue
        ids.update(cid for cid, t in normed if gn in t)
    return ids


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-generation", action="store_true")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--recall-k", type=int, default=20)
    ap.add_argument("--out", default="")
    ap.add_argument("--baseline", default="")
    args = ap.parse_args()

    items = [json.loads(l) for l in open(GOLDEN, encoding="utf-8") if l.strip()]
    con = sqlite3.connect("file:doc_management.db?mode=ro", uri=True)
    db = SessionLocal()

    val_items = [i for i in items if i["kind"] == "value"]
    none_items = [i for i in items if i["kind"] == "none"]

    recall_hits = hit5 = 0
    mrr_sum = 0.0
    per_q = []
    t0 = time.perf_counter()

    for it in val_items:
        gids = gold_chunk_ids(con, it["gold"])
        embed_q, doc_id = retrieval.resolve_spec_scope(db, it["q"])
        # 必須與 rag.py 走同一條查詢路徑（含 instruct 前綴），
        # 否則量到的不是實際行為。
        emb = ai.embed_query(embed_q)
        if not emb:
            continue

        # 第 1 層：撈寬（不 rerank），量混合檢索的召回
        wide = retrieval.hybrid_retrieve(db, it["q"], emb[0], args.recall_k,
                                         document_id=doc_id)
        wide_ids = [c.id for c, _ in wide]
        in_recall = bool(gids & set(wide_ids))

        # 第 2 層：正式 top_k（含 rerank），量排序
        top = retrieval.hybrid_retrieve(db, it["q"], emb[0], args.top_k,
                                        document_id=doc_id)
        top_ids = [c.id for c, _ in top]
        rank = next((i + 1 for i, cid in enumerate(top_ids) if cid in gids), 0)

        recall_hits += int(in_recall)
        hit5 += int(rank > 0)
        mrr_sum += (1.0 / rank) if rank else 0.0
        per_q.append({"id": it["id"], "q": it["q"], "recall": in_recall,
                      "rank": rank, "n_gold_chunks": len(gids)})

    n = len(val_items)
    result = {
        "n_value_questions": n,
        f"recall@{args.recall_k}": round(recall_hits / n, 3) if n else 0,
        f"hit@{args.top_k}": round(hit5 / n, 3) if n else 0,
        "mrr": round(mrr_sum / n, 3) if n else 0,
        "retrieval_seconds": round(time.perf_counter() - t0, 1),
    }

    print("=" * 62)
    print(f"檢索層（{n} 題）")
    print(f"  recall@{args.recall_k} = {result[f'recall@{args.recall_k}']}   "
          f"（正解有沒有進候選池 —— 進不去，後面再強也救不回來）")
    print(f"  hit@{args.top_k}      = {result[f'hit@{args.top_k}']}   （rerank 後有沒有排進前 {args.top_k}）")
    print(f"  MRR        = {result['mrr']}")
    print(f"  耗時 {result['retrieval_seconds']}s")
    print()
    misses = [p for p in per_q if not p["recall"]]
    if misses:
        print("  未進候選池的題目：")
        for p in misses:
            print(f"    {p['id']} {p['q'][:34]}（正解 {p['n_gold_chunks']} 塊）")
    ranked_low = [p for p in per_q if p["recall"] and not p["rank"]]
    if ranked_low:
        print(f"  進了候選池卻沒排進前 {args.top_k}（rerank 問題）：")
        for p in ranked_low:
            print(f"    {p['id']} {p['q'][:34]}")

    if args.with_generation:
        from app.services.agent import has_values  # noqa: PLC0415
        print()
        print("生成層（需呼叫 LLM，較慢）")
        grounded = hallucinated = 0
        t1 = time.perf_counter()
        for it in val_items:
            ans, _src = retrieval_answer(db, it["q"])
            ok = any(norm(g) in norm(ans) for g in it["gold"])
            grounded += int(ok)
        for it in none_items:
            ans, _src = retrieval_answer(db, it["q"])
            # 語料內不存在的題目，答案不該出現任何帶單位的數值
            hallucinated += int(has_values(ans))
        result["value_grounding_rate"] = round(grounded / n, 3) if n else 0
        result["hallucination_rate"] = round(hallucinated / len(none_items), 3) if none_items else 0
        result["generation_seconds"] = round(time.perf_counter() - t1, 1)
        print(f"  數值落地率 = {result['value_grounding_rate']}   （正解數值有沒有真的寫進答案）")
        print(f"  幻覺率     = {result['hallucination_rate']}   （語料內不存在的題目卻給出數值）")
        print(f"  耗時 {result['generation_seconds']}s")

    print("=" * 62)

    if args.baseline:
        try:
            base = json.load(open(args.baseline, encoding="utf-8"))
            print("與基線比較：")
            for k, v in result.items():
                if isinstance(v, (int, float)) and k in base and "seconds" not in k:
                    d = v - base[k]
                    mark = "↑" if d > 0 else ("↓" if d < 0 else "＝")
                    print(f"  {k:<22} {base[k]} → {v}  {mark}{abs(d):.3f}")
        except Exception as e:  # noqa: BLE001
            print(f"（無法讀取基線 {args.baseline}: {e}）")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({**result, "per_question": per_q}, f, ensure_ascii=False, indent=2)
        print(f"結果已寫入 {args.out}")

    db.close()
    con.close()


def retrieval_answer(db, question: str):
    """走與 /rag/query 相同的合成路徑，取得答案文字。"""
    from app.services.agent import run_rag_only  # noqa: PLC0415
    try:
        return run_rag_only(db, question)
    except Exception as e:  # noqa: BLE001
        return f"(生成失敗: {e})", []


if __name__ == "__main__":
    main()
