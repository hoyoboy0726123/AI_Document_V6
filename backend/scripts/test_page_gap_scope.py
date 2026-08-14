"""頁距「限同文件」判斷的回歸測試 —— 不需伺服器，純函式。

背景：rag.py 兩端點與 agent.py 合成原本計算 page_gap 時不看 document_id，
跨文件來源會被誤標「⚠️ 頁距 N，可能為不同章節」，再被提示詞規則勸退。
頁碼是文件內座標：A 文件第 3 頁與 B 文件第 250 頁之間不存在「頁距 247」，
所以跨文件（或頁碼/文件識別缺漏）一律回 None、不加警示；
同文件內的頁距照算，機制原本的防污染用途不變。

執行：cd backend && .venv\\Scripts\\python.exe scripts/test_page_gap_scope.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.services.retrieval import page_gap_within_doc  # noqa: E402
from app.services.ai import _page_label  # noqa: E402

FAIL = 0


def check(name, got, want):
    global FAIL
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL += 1


# 同文件：頁距照算（機制原本的用途不變）
check("同文件 3→14", page_gap_within_doc("docA", 3, "docA", 14), 11)
check("同文件同頁", page_gap_within_doc("docA", 3, "docA", 3), 0)
check("同文件大頁距仍要算（警示要觸發）", page_gap_within_doc("docA", 3, "docA", 250), 247)

# 跨文件：不算（本次修正的核心）
check("跨文件 3 vs 250", page_gap_within_doc("docA", 3, "docB", 250), None)

# 資訊不足：一律不算、不警示
check("候選缺頁碼", page_gap_within_doc("docA", 3, "docA", 0), None)
check("主命中缺頁碼", page_gap_within_doc("docA", 0, "docA", 14), None)
check("主命中缺文件識別", page_gap_within_doc(None, 3, "docA", 14), None)
check("候選缺文件識別", page_gap_within_doc("docA", 3, None, 14), None)

# 提示詞標籤層的整合行為（_page_label 是頁距唯一進提示詞的通道）
cross = _page_label({"page": 250, "page_gap": None}, 1)
check("跨文件標籤無 ⚠️ 警示", "⚠️" in cross, False)
check("跨文件標籤仍印頁碼", "第 250 頁" in cross, True)
far = _page_label({"page": 250, "page_gap": 247}, 1)
check("同文件大頁距有 ⚠️ 警示", "⚠️" in far, True)

print()
if FAIL:
    print(f"共 {FAIL} 項失敗")
    sys.exit(1)
print("全部通過（11 項）")
