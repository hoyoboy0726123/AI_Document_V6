# -*- coding: utf-8 -*-
"""驗證章節感知的 in_scope：同方法放行、跨方法擋掉、沒標籤時退回頁碼判斷。

要證明的是「章節比頁碼準」——±15 頁會犯兩種相反的錯：
  * 切掉同一個方法的後半段（方法跨了 20 頁）
  * 放進隔壁方法的開頭（兩個方法擠在相近頁碼）
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.services.agent import evidence_scope, evidence_sections, in_scope, _same_method  # noqa: E402

results = []


def check(name, ok, extra=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{('  ' + extra) if extra else ''}")


D = "doc-810h"
anchor = [{"document_id": D, "page": 300, "section_path": "METHOD 510.7"}]
scope = evidence_scope([(D, 300)])
secs = evidence_sections(anchor)

check("同一方法、超出 ±15 頁仍放行（方法跨 20 頁不該被切半）",
      in_scope(scope, D, 325, secs, "METHOD 510.7") is True)
check("不同方法、頁碼很近仍擋掉（±15 頁擋不住的那種污染）",
      in_scope(scope, D, 305, secs, "METHOD 508.8") is False)
check("方法自己的 ANNEX 視為同一方法",
      in_scope(scope, D, 340, secs, "METHOD 510.7, ANNEX A") is True)
check("_same_method：本體 vs ANNEX", _same_method("METHOD 504.3", "METHOD 504.3, ANNEX A") is True)
check("_same_method：不同方法", _same_method("METHOD 504.3", "METHOD 505.7") is False)

# 沒有章節標籤的文件（41 份裡有 40 份）必須完全維持原本的頁碼行為
no_sec = evidence_sections([{"document_id": D, "page": 300, "section_path": None}])
check("無章節標籤時退回頁碼：範圍內放行", in_scope(scope, D, 310, no_sec, None) is True)
check("無章節標籤時退回頁碼：範圍外擋掉", in_scope(scope, D, 400, no_sec, None) is False)
check("有標籤的錨點但候選沒標籤 → 退回頁碼", in_scope(scope, D, 310, secs, None) is True)
check("別的文件一律擋掉", in_scope(scope, "other-doc", 300, secs, "METHOD 510.7") is False)
check("scope 為空時放行（範圍未知不亂擋）", in_scope({}, D, 999, secs, "METHOD 508.8") is True)

print()
print("全部通過 ✅" if all(results) else f"有 {results.count(False)} 項失敗 ❌")
sys.exit(0 if all(results) else 1)
