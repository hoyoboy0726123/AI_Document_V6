"""分類型品質評測：數值題以外的七種題型。

為什麼另開一支
--------------
run_eval.py 只量數值題（30 題）與不存在題（5 題）。但使用者實際會問的還有
列舉、規範關係、目的、跨文件比較，以及兩種最危險的問法：

  * 跨方法陷阱：問 A 方法一個只有 B 方法才有的參數。正確行為是說「A 沒有
    這項規定」，錯誤行為是把 B 的數值搬過來 —— 這是實測發生過的污染方向
    （沙塵測試被答成黴菌測試的培養溫度），而且答案看起來完全合理。
  * 假前提：問句本身內含編造的事實（「METHOD 599」「第 12 版」）。模型的
    預設行為是替假前提找理由，而不是指出它不存在。

每一型都有機器可判的評分標準，不需要人工看答案，才能當回歸測試重複跑。

評分方式（各型不同，混在一起平均沒有意義）
------------------------------------------
  enum / rel_ref / rel_sup / crossdoc  → gold 項目的覆蓋率（recall）
  purpose                              → 關鍵詞命中數 ≥ min_hit
  cross_method / false_premise         → 「有沒有誠實說沒有」的通過率（不得給數值）
  followup                             → 第一輪答對 且 第二輪仍在同一個主體

用法：
    python scripts/eval/run_eval_types.py            # 全部題型
    python scripts/eval/run_eval_types.py --kind enum,purpose
    python scripts/eval/run_eval_types.py --out scripts/eval/types_baseline.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict

sys.path.insert(0, ".")

from app.database import SessionLocal  # noqa: E402

GOLDEN = "scripts/eval/golden_types.jsonl"

# 與 run_eval.py 共用同一組「誠實說查不到」的判準，避免兩支評測對同一個
# 行為給出不同的判定。
sys.path.insert(0, "scripts/eval")
from run_eval import is_honest_no_answer, norm  # noqa: E402
from app.services.agent import has_values  # noqa: E402


def _hits(golds: list, text: str) -> int:
    """gold 項目在答案中出現幾個（正規化後的子字串比對）。

    規範編號要寬鬆比對：答案寫 "MIL-STD-1472G" 應該算命中 gold "MIL-STD-1472"，
    因為版本尾碼是規範本身的細節，不是答對與否的分野。
    """
    t = norm(text)
    return sum(1 for g in golds if norm(g) in t)


def _mentions_subject(subjects: list, text: str) -> bool:
    t = norm(text)
    return any(norm(s) in t for s in subjects)


# 「這份規範沒有這項規定」的各種說法。陷阱題的正確答案不一定是標準兜底句，
# 也可能是這種正面敘述，兩者都該算通過。
_DENIAL_RE = re.compile(
    r"(未(?:有)?(?:針對|規定|提及|說明|提供|列出)|沒有(?:相關|這|此|針對)|不適用|"
    r"查無|無法(?:在|從).{0,12}找到|並(?:未|沒有)(?:規定|提及|包含|涵蓋)|"
    r"不屬於|不存在|not (?:specified|applicable|found)|"
    # 系統的確定性標註：問句前提查不到依據、以及主體一致性未過時的回覆。
    # 這兩種都是正確行為，評分器必須認得，否則修好了也量不出來。
    r"未能在.{0,20}找到|不代表規範確有此規定|屬於其他測試項目|可能未規定此項)")


def _refused(text: str, forbidden: list | None = None) -> bool:
    """陷阱題的通過條件。

    第一版只看「有沒有數值」，結果 x03 假通過：答案照樣講了沙塵測試的
    黴菌菌種（污染內容），只是沒帶單位數字，就被判成正確拒答。
    改成三選一才算通過：
      1. 標準兜底句
      2. 明確否認（「規範中未針對淋雨測試規定鹽溶液濃度」）
      3. 既沒有數值、也沒有出現該參數的關鍵詞（真的沒答那件事）
    """
    if is_honest_no_answer(text) or _DENIAL_RE.search(text or ""):
        return True
    if has_values(text):
        return False
    t = norm(text)
    return not any(norm(f) in t for f in (forbidden or []))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", help="只跑指定題型（逗號分隔）")
    ap.add_argument("--out", help="輸出 JSON")
    ap.add_argument("--baseline", help="與先前結果比較")
    ap.add_argument("--show", action="store_true", help="印出每題答案開頭")
    args = ap.parse_args()

    items = [json.loads(l) for l in open(GOLDEN, encoding="utf-8") if l.strip()]
    if args.kind:
        want = {k.strip() for k in args.kind.split(",")}
        items = [i for i in items if i["kind"] in want]

    from app.services.agent import route_mode, run_agent, run_rag_only  # noqa: PLC0415

    def answer(question, history=None, route="rag"):
        """依題型走對應的管線。

        路由一律交給系統自己的 route_mode()，不由評測手動標註 —— 手動標註只能
        驗證「送對分支時答得對不對」，驗不到「系統會不會送錯分支」。
        實測列舉題就是被送錯的：「METHOD 514.8 底下有哪些 ANNEX」因為
        「有哪些」與「ANNEX」之間的空格而不匹配 _ENUM_STRUCT_RE，被送去 RAG，
        KG 裡完整的 ANNEX A–F 清單從來沒被用到。手動標 route=agent 會把
        這個 bug 蓋掉。
        """
        if route != "agent":
            # 三元運算子的優先序低於逗號：寫成 `a if c else b, None` 會讓 c 為真時
            # 回傳整個 tuple（第一版就是這樣炸掉的）。拆開寫最清楚。
            text, _src = run_rag_only(db, question, history) if history else run_rag_only(db, question)
            return text, None
        final = ""
        for evt in run_agent(db, question, conversation_history=history, max_steps=6):
            if evt.get("type") == "final":
                final = evt.get("text") or ""
        return final, None

    db = SessionLocal()
    per_kind = defaultdict(lambda: {"n": 0, "score": 0.0, "fail": []})
    t0 = time.time()
    try:
        for it in items:
            kind, qid = it["kind"], it["id"]
            try:
                ans, _ = answer(it["q"], route=route_mode(it["q"]))
            except Exception as exc:  # noqa: BLE001
                print(f"  [ERR] {qid} {exc}")
                per_kind[kind]["n"] += 1
                per_kind[kind]["fail"].append(f"{qid} 執行失敗")
                continue
            ans = ans or ""

            if kind in ("enum", "rel_ref", "rel_sup", "crossdoc"):
                golds = it["gold"]
                score = _hits(golds, ans) / len(golds) if golds else 0.0
                # crossdoc 另外要求主題詞至少中一個，避免只是複誦兩個編號
                if kind == "crossdoc" and it.get("gold_any"):
                    if not _hits(it["gold_any"], ans):
                        score = 0.0
                ok = score >= (0.5 if kind in ("rel_ref", "rel_sup") else 0.999)
            elif kind == "purpose":
                score = 1.0 if _hits(it["gold"], ans) >= it.get("min_hit", 1) else 0.0
                ok = score > 0
            elif kind in ("cross_method", "false_premise"):
                score = 1.0 if _refused(ans, it.get("forbidden")) else 0.0
                ok = score > 0
            elif kind == "followup":
                first_ok = _hits(it["gold"], ans) > 0
                try:
                    # 對話歷史的真實欄位是 question/answer（見 api/v1/rag.py），
                    # 不是 OpenAI 風格的 role/content。第一版用錯格式，
                    # _history_subject 讀不到任何東西，量到的「追問跑題」
                    # 有一部分是評測自己造成的。
                    hist = [{"question": it["q"], "answer": ans}]
                    ans2, _ = answer(it["followup"], hist, route_mode(it["followup"]))
                except Exception as exc:  # noqa: BLE001
                    ans2 = f"(追問失敗: {exc})"
                second_ok = _mentions_subject(it["gold_followup_subject"], ans2 or "")
                score = (first_ok + second_ok) / 2
                ok = first_ok and second_ok
                if args.show:
                    print(f"    追問答案：{(ans2 or '')[:100]}")
            else:
                continue

            per_kind[kind]["n"] += 1
            per_kind[kind]["score"] += score
            mark = "OK" if ok else ("部分" if score > 0 else "FAIL")
            print(f"  [{mark:4s}] {qid} {it['q'][:34]:36s} {score:.2f}")
            if args.show:
                print(f"    → {re.sub(chr(92) + 's+', ' ', ans)[:120]}")
            if not ok:
                per_kind[kind]["fail"].append(f"{qid} {it['q'][:30]}（{score:.2f}）")

        print("\n" + "=" * 62)
        print("分型結果（各型評分標準不同，不要平均在一起看）")
        result = {}
        for kind in ("enum", "rel_ref", "rel_sup", "purpose",
                     "cross_method", "false_premise", "crossdoc", "followup"):
            d = per_kind.get(kind)
            if not d or not d["n"]:
                continue
            avg = d["score"] / d["n"]
            result[kind] = round(avg, 3)
            print(f"  {kind:14s} {avg:.3f}   （{d['n']} 題）")
            for f in d["fail"]:
                print(f"       未過：{f}")
        result["seconds"] = round(time.time() - t0, 1)
        print(f"\n耗時 {result['seconds']}s")

        if args.baseline:
            base = json.load(open(args.baseline, encoding="utf-8"))
            print("\n與基線比較：")
            for k, v in result.items():
                if k in base and isinstance(v, (int, float)):
                    d = v - base[k]
                    print(f"  {k:14s} {base[k]} → {v}  {'＝' if abs(d) < 1e-9 else f'{d:+.3f}'}")
        if args.out:
            json.dump(result, open(args.out, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
            print(f"結果已寫入 {args.out}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
