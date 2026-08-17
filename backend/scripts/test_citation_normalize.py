"""引用標記正規化 —— 不需伺服器、不呼叫模型。

提示詞要求半形 `[來源N]`，但雲端模型（實測 AiHub gpt-oss）會自行改用全形
【來源N】。而 citation_check._MARKER_RE、評測腳本、ai._strip_citations
三處都只認半形，於是：

- 引用事後校驗變成靜默空轉（沒有錯誤、沒有警告，使用者卻以為驗證過了）
- 追問時歷史裡的舊來源編號不會被淨化，會被原封不動抄進新一輪
- golden set 的 citation 題型直接判 0 分（實測 u14 從 0.50 掉到 0.00，
  只因為那一輪模型改用了全形）

執行：cd backend && .venv\\Scripts\\python.exe scripts/test_citation_normalize.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.services.ai import _CITATION_RE, normalize_citation_markers  # noqa: E402
from app.services.citation_check import _MARKER_RE                    # noqa: E402

FAIL = 0


def check(name, got, want):
    global FAIL
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL += 1


# 正規化：全形轉半形，編號要保住
check("單一全形", normalize_citation_markers("見【來源1】。"), "見[來源1]。")
check("兩位數編號", normalize_citation_markers("【來源12】"), "[來源12]")
check("標記內有空白", normalize_citation_markers("【來源 7 】"), "[來源7]")
check("多個一起", normalize_citation_markers("【來源1】【來源2】"), "[來源1][來源2]")
check("半形不動", normalize_citation_markers("已是 [來源3]"), "已是 [來源3]")
check("混用", normalize_citation_markers("【來源2】和 [來源4]"), "[來源2]和 [來源4]")

# 不可誤傷一般的全形方頭括號
check("一般全形【重要】不動",
      normalize_citation_markers("注意【重要】事項"), "注意【重要】事項")
check("【來源】無編號不動",
      normalize_citation_markers("見【來源】"), "見【來源】")

# 邊界
check("空字串", normalize_citation_markers(""), "")
check("None 視為空", normalize_citation_markers(None), "")

# 正規化後，下游的校驗正規式必須認得
check("正規化後 citation_check 認得",
      _MARKER_RE.findall(normalize_citation_markers("【來源1】與【來源2】")),
      ["1", "2"])

# 歷史淨化：兩種寫法都要認（避免舊編號被抄進新一輪）
check("_strip_citations 認全形", bool(_CITATION_RE.search("【來源2】")), True)
check("_strip_citations 認半形", bool(_CITATION_RE.search("[來源2]")), True)

# 內容不變性：除了標記本身，一個字都不能動
src = "溫度為 23 ± 2 °C【來源1】，濕度 50 ± 5% RH [來源2]。表格見下。"
out = normalize_citation_markers(src)
check("非標記內容完全不變",
      out.replace("[來源1]", "").replace("[來源2]", ""),
      src.replace("【來源1】", "").replace("[來源2]", ""))

print()
if FAIL:
    print(f"共 {FAIL} 項失敗")
    sys.exit(1)
print("全部通過")
