"""查詢改寫行為探針：直接呼叫 analyze_followup_intent，隔離量測提示詞改動。

執行：.venv\Scripts\python.exe scripts\probe_query_rewrite.py（需 Ollama）

改 analyze_followup_intent 的提示詞前後各跑一次。2026-08-12 基準：
  舊提示詞（RULE 1 無條件冠前題主體）3/6 —— 自帶主詞的三案全被污染，
  例如「料件有哪些種類」被改成「buying mode 料件種類 分類」
  新提示詞（先判自帶主詞、省略型才繼承）6/6

六個案例分兩類：
  自帶主詞（期望：原樣保留，不冠前題主體）
  省略型追問（期望：繼承前題主體 —— 這是改寫存在的理由，不能退化）
"""
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"D:\AI_Projects\RAG\AI_Document_V6\backend")

from app.services import ai  # noqa: E402

CASES = [
    # (前題, 現題, 期望類型)  期望類型: keep=原樣保留 / inherit=繼承主體
    ("buying mode有幾種", "料件有哪些種類", "keep"),          # 使用者實際踩到的
    ("鹽霧測試的箱體溫度要維持在幾度", "冷凝測試方法與條件", "keep"),  # README 記錄的誤判案例
    ("料件有哪些種類", "新版原物料料號總共幾碼", "keep"),
    ("buying mode有幾種", "那 C 是什麼", "inherit"),
    ("鹽霧測試的箱體溫度要維持在幾度", "那濕度呢", "inherit"),
    ("What are ESD test steps?", "limit", "inherit"),
]

ok = 0
for prev_q, cur_q, want in CASES:
    hist = [{"question": prev_q, "answer": "（前題答案略）"}]
    r = ai.analyze_followup_intent(cur_q, hist)
    opt = (r.get("optimized_query") or "").strip()
    kept = opt == cur_q
    if want == "keep":
        passed = kept
        verdict = "保留原句" if kept else f"被改寫 →「{opt}」"
    else:
        # 繼承 = 改寫後包含前題主體的關鍵詞（粗略判斷：前題的頭幾個非停用詞）
        passed = (not kept) and any(
            t.lower() in opt.lower()
            for t in [prev_q.split("有")[0], "buying", "鹽霧", "esd", "salt"]
            if len(t) >= 2)
        verdict = f"「{opt}」"
    ok += passed
    print(f"  [{'PASS' if passed else 'FAIL'}] ({want:7}) 「{cur_q}」 → {verdict}")

print(f"\n  {ok}/{len(CASES)} 符合期望")
