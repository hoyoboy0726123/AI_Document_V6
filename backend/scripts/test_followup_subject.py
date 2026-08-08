# -*- coding: utf-8 -*-
"""驗證追問補主體：省略主體要繼承，新問題不可被改寫。

最重要的是「不誤傷」——左側面板的新問題不帶對話歷史，任何情況下都不該被改寫。
「冷凝測試方法與條件」被誤當成追問而繼承了上一輪的「濕度」，是實際發生過的 bug。
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.services.agent import resolve_followup_question as R  # noqa: E402

results = []


def check(name, ok, extra=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{('  ' + extra) if extra else ''}")


H = [{"question": "鹽霧測試的箱體溫度要維持在幾度", "answer": "應維持在 35 ±1 ºC（95 ±3.6 ºF）"}]
HF = [{"question": "黴菌測試量測濕球溫度時，通過濕球的風速至少要多少", "answer": "不得低於 4.6 m/s"}]

r = R("那濕度呢", H)
check("參數型追問補上主體（「濕度」不再被誤認為測試名）", "鹽霧測試" in r and "濕度" in r, f"→ {r}")
r = R("這個測試要跑多久", HF)
check("無主體追問補上主體", "黴菌測試" in r, f"→ {r}")
r = R("要跑幾個循環", H)
check("無主體短問句補上主體", "鹽霧測試" in r, f"→ {r}")

# 不誤傷
check("沒有歷史時完全不改寫（左側新問題）", R("冷凝測試方法與條件", None) == "冷凝測試方法與條件")
check("空歷史也不改寫", R("那濕度呢", []) == "那濕度呢")
r = R("冷凝測試方法與條件", H)
check("追問若自己指名了別的測試，不可被上一輪蓋掉", r == "冷凝測試方法與條件", f"→ {r}")
r = R("MIL-STD-331D 的落錘高度是多少", H)
check("追問帶規範編號時不改寫", r == "MIL-STD-331D 的落錘高度是多少", f"→ {r}")
r = R("濕度測試的建議持續天數是多少", H)
check("帶「測試」後綴的參數詞是主體，不改寫", r == "濕度測試的建議持續天數是多少", f"→ {r}")
r = R("請詳細說明整份規範對於環境工程剪裁流程的六個步驟分別是什麼", H)
check("長問句即使無主體也不硬套", "鹽霧" not in r, f"→ {r[:30]}…")
check("空字串安全", R("", H) == "")

print()
print("全部通過 ✅" if all(results) else f"有 {results.count(False)} 項失敗 ❌")
sys.exit(0 if all(results) else 1)
