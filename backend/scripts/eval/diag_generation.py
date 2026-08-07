"""診斷生成層失敗：檢索成功但答案沒寫出數值的那些題。

生成層量測顯示 hit@5=0.733 但數值落地率只有 0.533 —— 22 題的正解進了前 5，
只有 16 題把數值寫進答案。那 6 題的差距要分辨成三種原因，修法完全不同：

  A. 正解進了 top_k，卻沒進 context_keep_ids  → 根本沒送到模型面前（脈絡挑選問題）
  B. 進了脈絡，但數值被預算截斷              → 脈絡預算／擴展問題
  C. 數值就在脈絡裡，模型卻沒寫出來          → 提示詞或模型能力問題

用法：python scripts/eval/diag_generation.py
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys

sys.path.insert(0, ".")

from app.database import SessionLocal  # noqa: E402
from app.services import ai, retrieval  # noqa: E402

GOLDEN = "scripts/eval/golden.jsonl"


def norm(v: str) -> str:
    s = re.sub(r"\s+", "", v or "").lower()
    return s.replace("º", "°").replace("³", "3").replace("²", "2")


def main() -> None:
    items = [json.loads(l) for l in open(GOLDEN, encoding="utf-8") if l.strip()]
    vals = [i for i in items if i["kind"] == "value"]
    con = sqlite3.connect("file:doc_management.db?mode=ro", uri=True)
    db = SessionLocal()

    rows = con.execute("SELECT id, text FROM document_chunks").fetchall()
    normed = [(cid, norm(t)) for cid, t in rows]

    buckets = {"A_不在脈絡": [], "B_被截斷": [], "C_模型沒寫": [], "OK": [], "檢索就失敗": []}

    for it in vals:
        gids = {cid for cid, t in normed
                for g in it["gold"] if norm(g) and norm(g) in t}
        eq, doc_id = retrieval.resolve_spec_scope(db, it["q"])
        emb = ai.embed_query(eq)
        if not emb:
            continue
        top = retrieval.hybrid_retrieve(db, it["q"], emb[0], 5, document_id=doc_id)
        top_ids = [c.id for c, _ in top]
        if not (gids & set(top_ids)):
            buckets["檢索就失敗"].append(it["id"])
            continue

        # 走與 /rag/query 相同的脈絡挑選
        keep = retrieval.context_keep_ids(top)
        gold_in_keep = bool(gids & keep)

        ctx_used = 0
        gold_ctx_text = ""
        for chunk, _s in top:
            if chunk.id not in keep:
                continue
            text = retrieval.context_text_budgeted(db, chunk, ctx_used)
            if not text:
                continue
            ctx_used += len(text)
            if chunk.id in gids:
                gold_ctx_text = text

        has_val_in_ctx = any(norm(g) in norm(gold_ctx_text) for g in it["gold"])

        if not gold_in_keep:
            buckets["A_不在脈絡"].append(it["id"])
            continue
        if not has_val_in_ctx:
            buckets["B_被截斷"].append(it["id"])
            continue

        from app.services.agent import run_rag_only  # noqa: PLC0415
        ans, _ = run_rag_only(db, it["q"])
        if any(norm(g) in norm(ans) for g in it["gold"]):
            buckets["OK"].append(it["id"])
        else:
            buckets["C_模型沒寫"].append(it["id"])
            print(f"\n--- {it['id']} {it['q']} ---")
            print(f"  正解: {it['gold']}")
            i = norm(gold_ctx_text).find(norm(it["gold"][0]))
            raw_i = max(0, int(i * len(gold_ctx_text) / max(1, len(norm(gold_ctx_text)))) - 90)
            print(f"  脈絡片段: …{re.sub(r'（s）+', ' ', gold_ctx_text[raw_i:raw_i + 220])}…")
            print(f"  答案: {ans[:200]}")

    print("\n" + "=" * 58)
    for k, v in buckets.items():
        print(f"  {k:<12} {len(v):>2} 題  {v}")
    print("=" * 58)
    print("\n判讀：A 要修脈絡挑選、B 要修預算、C 要修提示詞或換模型")
    db.close()
    con.close()


if __name__ == "__main__":
    main()
