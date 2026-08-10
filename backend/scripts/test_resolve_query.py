# -*- coding: utf-8 -*-
"""驗證查詢改寫的單一入口 resolve_query。

會呼叫 LLM，比其他單元測試慢（約 1-2 分鐘）。

設計重點是「無條件改寫」取代「先分類再決定」——分類一定要有門檻，
而門檻一定會錯：實測「溫度有確切的數值嗎 你提供的是允許公差」超過 18 字
被判成新問題，但使用者實際是在追問。
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.services.agent import resolve_query, _find_subject  # noqa: E402

results = []


def check(name, ok, extra=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{('  ' + extra) if extra else ''}")


H = [{"question": "鹽霧測試的箱體溫度要維持在幾度", "answer": "應維持在 35 +1/−2 ºC（95 +2/−3 ºF）"}]

# ── 沒有歷史 → 原樣 ──
r = resolve_query("鹽霧測試的箱體溫度", None)
check("第一題不改寫", r["query"] == "鹽霧測試的箱體溫度" and not r["rewritten"])
check("空歷史不改寫", resolve_query("x", [])["rewritten"] is False)

# ── 快速路徑：自帶規範編號 → 不呼叫 LLM ──
r = resolve_query("MIL-STD-331D 的落錘高度是多少", H)
check("自帶規範編號走快速路徑", r["source"] == "self_contained" and not r["rewritten"],
      f"→ source={r['source']}")
r = resolve_query("METHOD 509.7 的曝露時間", H)
check("自帶方法號走快速路徑", r["source"] == "self_contained")

# ── 省略主體的追問 → 應補上主體 ──
r = resolve_query("那濕度呢", H)
check("省略主體的追問會被改寫", r["rewritten"] and "鹽霧" in r["query"],
      f"→ {r['query']}")

# ── 使用者實際踩到的案例：長度超過舊門檻但確實是追問 ──
r = resolve_query("溫度有確切的數值嗎 你提供的是允許公差", H)
check("長追問也會補上主體（舊的 18 字門檻會判錯）",
      r["rewritten"] and "鹽霧" in r["query"], f"→ {r['query']}")

# ── 語言一致性：中文問句不該被改成整句英文 ──
import re  # noqa: E402
cn = lambda t: bool(re.search(r"[一-鿿]", t))  # noqa: E731
r = resolve_query("那濕度呢", H)
check("中文問句改寫後仍是中文", cn(r["query"]), f"→ {r['query']}")

# ── 主體抽取不可切出詞的碎片 ──
check("「詳細的測試條件」不可切出「細的」", _find_subject("詳細的測試條件") != "細的",
      f"→ {_find_subject('詳細的測試條件')}")
check("正常測試名仍抽得到", _find_subject("傾斜衝擊測試的測試條件") == "傾斜")

print()
print("全部通過 ✅" if all(results) else f"有 {results.count(False)} 項失敗 ❌")
sys.exit(0 if all(results) else 1)
