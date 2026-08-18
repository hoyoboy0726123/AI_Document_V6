# -*- coding: utf-8 -*-
"""多跳題組：需要「跨兩張表 join」才答得出來的問題。

為什麼要單獨一組：Unit0 題組（13 題）在「有 KG / 無 KG」下逐題分數完全相同 ——
那些題目單張表就答得完，向量檢索撈到表格後 LLM 直接讀就夠，KG 是重複的能力。
KG 真正該贏的地方是「答案不在任何單一段落裡」：
    頁8「Buying Mode → 料號」 ⋈ 頁11「料號前兩碼 → 大類名稱」
這種 join 沒有任何一塊 chunk 同時含有兩邊，向量檢索本質上撈不到完整證據。

用法（同一份題組跑兩次，中間切換 KG 有無）：
    python scripts/eval/run_eval_multihop.py --out with_kg.json
    curl -X DELETE .../kg/tables/extraction/料件大類
    python scripts/eval/run_eval_multihop.py --out without_kg.json
"""
import argparse
import json
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import logging  # noqa: E402
logging.disable(logging.WARNING)

from app.main import apply_llm_overrides_from_db  # noqa: E402
apply_llm_overrides_from_db()
from app.database import SessionLocal  # noqa: E402
from app.services.agent import route_mode, run_agent, run_rag_only  # noqa: E402

GOLDEN = pathlib.Path(__file__).with_name("golden_multihop.jsonl")


def norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "").upper()


def answer(db, q: str) -> str:
    """走混合路由 —— 與使用者實際查詢同一條路。"""
    if route_mode(q, db) == "agent":
        out = ""
        for ev in run_agent(db, q):
            if ev.get("type") == "final":
                out = ev.get("text") or ""
        return out
    text, _src = run_rag_only(db, q)
    return text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    items = [json.loads(l) for l in GOLDEN.read_text(encoding="utf-8").splitlines() if l.strip()]
    db = SessionLocal()
    per_q, total = [], 0.0
    t0 = time.perf_counter()
    for it in items:
        t = time.perf_counter()
        try:
            a = answer(db, it["q"])
        except Exception as e:  # noqa: BLE001
            a = f"(執行失敗: {e})"
        na = norm(a)
        if it.get("match") == "count_phrase":
            # 計數題：必須是「共 N 個 / 總共 N 項」這種量詞形狀。
            # 用字串包含會被答案表格裡任何一個相同數字蒙混過關（實測 m05
            # 答「共計 16 個代號」卻因別處出現 30 而被判對）。
            import re as _re
            # 「共有」原本漏掉（共 後面卡個 有 就不匹配）——量測工具第 16 次出錯。
            # pattern 與 run_eval_count.py 對齊。
            found = _re.findall(r"(?:共計|總共|共有|共|合計|一共)\s*(?:有\s*)?(\d{1,4})\s*(?:個|項|筆|種|類)", a or "")
            hit = [g for g in it["gold"] if g in found]
        else:
            hit = [g for g in it["gold"] if norm(g) in na]
        score = 1.0 if len(hit) >= it.get("min_hit", len(it["gold"])) else round(len(hit) / max(len(it["gold"]), 1), 2)
        total += score
        per_q.append({"id": it["id"], "kind": it["kind"], "q": it["q"],
                      "score": score, "hit": hit,
                      "missing": [g for g in it["gold"] if g not in hit],
                      "len": len(a), "secs": round(time.perf_counter() - t, 1)})
        print(f"  {it['id']}  {score:>4}  命中 {len(hit)}/{len(it['gold'])}  "
              f"{per_q[-1]['secs']:>5}s  {it['q'][:34]}", flush=True)
        if score < 1.0:
            print(f"        缺: {per_q[-1]['missing']}", flush=True)
    db.close()

    result = {"n": len(items), "score": round(total / max(len(items), 1), 3),
              "seconds": round(time.perf_counter() - t0, 1), "per_question": per_q}
    print(f"\n  總分 {result['score']}   耗時 {result['seconds']}s")
    if args.out:
        pathlib.Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  已寫出 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
