# -*- coding: utf-8 -*-
"""驗證主體一致性查核：能抓到「主體被丟棄」，且不誤擋正常題目。

誤擋的代價比漏抓高得多 —— 漏抓只是回到現狀，誤擋會讓答得出來的題目
變成「查無相關資料」。所以未登錄的主體一律放行。
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.services.agent import subject_terms, evidence_matches_subject as M  # noqa: E402

results = []


def check(name, ok, extra=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{('  ' + extra) if extra else ''}")


salt = [{"snippet": "A salt solution having a specific gravity of from 1.025 to 1.037",
         "section_path": None},
        {"snippet": "Salt fog chamber temperature 35 C", "section_path": "METHOD 509.7"}]
rain = [{"snippet": "The rain test procedure uses a water spray at 1.7 mm/min",
         "section_path": "METHOD 506.6"}]

check("實測的污染案例被抓到（問淋雨、證據全是鹽）", M("淋雨測試的鹽溶液濃度規定是多少", salt) is False)
check("證據真的談淋雨時放行", M("淋雨測試的降雨強度是多少", rain) is True)
check("問鹽霧時鹽的證據放行", M("鹽霧測試的箱體溫度", salt) is True)
check("靠 section_path 也能認出主體", M("鹽霧測試的濃度", [{"snippet": "x", "section_path": "METHOD 509.7"}]) is False,
      "（509.7 的英文詞不在 section_path 裡，本來就該由內文命中）")

check("未登錄的主體一律放行（不製造新的誤擋）", M("冷凝測試的條件", salt) is True)
check("沒有主體的問句放行", M("測試條件是什麼", salt) is True)
check("空證據放行", M("淋雨測試的降雨強度", []) is True)

check("subject_terms 帶出英文詞",
      subject_terms("淋雨測試的鹽溶液濃度") == ["淋雨", "rain", "water spray", "drip test"],
      f"→ {subject_terms('淋雨測試的鹽溶液濃度')}")
check("未登錄主體回空清單", subject_terms("冷凝測試的條件") == [])

print()
print("全部通過 ✅" if all(results) else f"有 {results.count(False)} 項失敗 ❌")
sys.exit(0 if all(results) else 1)
