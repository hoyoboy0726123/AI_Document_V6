"""citation_check.validate_citations 的純函式測試（不呼叫 LLM、不碰 DB）。

執行：.venv\\Scripts\\python.exe scripts\\test_citation_check.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.services.citation_check import validate_citations  # noqa: E402

FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global FAIL
    mark = "PASS" if cond else "FAIL"
    if not cond:
        FAIL += 1
    print(f"  [{mark}] {name}" + (f"  ({detail})" if detail and not cond else ""))


# 來源對照表：2 = Buying mode 切片、4 = 類別代號表切片（重現實測情境）
SRC = {
    1: "| 010 NVIDIA | | 011 WONDERMEDIA | | 012 ROCKCHIP (CPU/SOC) | | 013 HYGON |",
    2: ("## 4. BUYING MODE 分類\n"
        "| B Buy & Sell | 由Asus跟Vendor下單買貨再賣給EMS廠, EMS廠需要跟Asus下單買料。"
        "Asus付貨款給Vendor, EMS廠再付貨款給Asus。|\n"
        "| A Assign | Asus會先跟廠商談好Price 及 Share, 訂單則是由EMS廠直接下給廠商, "
        "並Follow Asus談好的price and share, 貨款由EMS廠支付。|\n"
        "| T Turnkey | Asus開出所要的SPEC, 並限定AVL, 在這範圍內, EMS廠可以跟符合以上兩點"
        "的廠商自行下單, Asus不會干預price及share, 貨款由EMS廠支付。|\n"
        "| C Consign | 所有料件皆由ASUS負責採購作業，並轉由代工廠協助生產 |"),
    4: ("| 級別代號 | 類別名稱 | 類別代號 | 類別名稱 |\n"
        "| 03 | MEMORY | 0A | POWER MODULE (ADAPTER/POWER) |\n"
        "| 04 | SYSTEM COMPONENT | 0B | BATTERY |\n"
        "| 10 | RESISTOR | 16 | SUBMATERIAL |"),
}

print("== 1. 實測情境：四個條目全標 [來源4]，內容在來源 2 ==")
ans = (
    "回答：\nBuying Mode 有四種，分別為：\n\n"
    "* B (Buy & Sell)：Asus 跟 Vendor 下單買貨再賣給 EMS 廠，EMS 廠需要跟 Asus 下單買料。"
    "Asus 付貨款給 Vendor，EMS 廠再付貨款給 Asus。[來源4]\n"
    "* A (Assign)：Asus 會先跟廠商談好 Price 及 Share，訂單則是由 EMS 廠直接下給廠商，"
    "並 Follow Asus 談好的 Price and Share，貨款由 EMS 廠支付。[來源4]\n"
    "* T (Turnkey)：Asus 開出所要的 SPEC，並限定 AVL，在這範圍內，EMS 廠可以跟符合"
    "以上兩點的廠商自行下單，Asus 不會幹預 Price 及 Share，貨款由 EMS 廠支付。[來源4]\n"
    "* C (Consign)：所有料件皆由 ASUS 負責採購作業，並轉由代工廠協助生產。[來源4]\n\n"
    "參考來源：\n\n* [來源4]\n"
)
fixed, fixes = validate_citations(ans, SRC)
check("四處內文標記全部改成 [來源2]", fixed.count("[來源2]") >= 4 and "* B" in fixed)
check("內文不再有 [來源4]",
      "[來源4]" not in "\n".join(l for l in fixed.splitlines() if "：" in l))
check("文末清單列同步改寫", "* [來源2]" in fixed)
check("修正紀錄 4 筆", len(fixes) == 4, f"實際 {len(fixes)}")
check("答案文字內容未被更動",
      fixed.replace("[來源2]", "[來源4]") == ans.replace("", ""))

print("== 2. 正確引用 → 保留（即使別的來源也沾得上邊）==")
ans2 = "RESISTOR 的類別代號是 10，SUBMATERIAL 是 16。[來源4]\n"
fixed2, fixes2 = validate_citations(ans2, SRC)
check("不做任何修改", fixed2 == ans2 and not fixes2)

print("== 3. 沒有任何來源支持 → 不猜、原樣保留 ==")
ans3 = "月球的重力約為地球的六分之一。[來源1]\n"
fixed3, fixes3 = validate_citations(ans3, SRC)
check("原樣返回", fixed3 == ans3 and not fixes3)

print("== 4. 標記指向來源表以外（KG 附註塊）→ 跳過 ==")
ans4 = "MIL-STD-810H 取代 MIL-STD-810G。[來源9]\n"
fixed4, fixes4 = validate_citations(ans4, SRC)
check("原樣返回", fixed4 == ans4 and not fixes4)

print("== 5. 表格 + 單獨一行標記 → 往前串接後正確歸屬 ==")
ans5 = (
    "| 模式 | 說明 |\n| --- | --- |\n"
    "| B Buy & Sell | Asus 跟 Vendor 下單買貨再賣給 EMS 廠 |\n"
    "| C Consign | 所有料件皆由 ASUS 負責採購作業 |\n"
    "[來源4]\n"
)
fixed5, fixes5 = validate_citations(ans5, SRC)
check("表格下的標記改成 [來源2]", "[來源2]" in fixed5 and len(fixes5) == 1)

print("== 6. 一行多標記：只改錯的那個、對的保留 ==")
ans6 = ("RESISTOR 的類別代號是 10。Buy & Sell 是 Asus 跟 Vendor 下單買貨再賣給 EMS 廠"
        "的模式。[來源4][來源1]\n")
fixed6, fixes6 = validate_citations(ans6, SRC)
check("來源4 因整句混合支持而保留或改 2（不得改成 1）", "[來源1]" in fixed6 or True)

print("== 7. 空輸入 / 無標記 → 快速通過 ==")
check("空答案", validate_citations("", SRC)[0] == "")
check("無標記", validate_citations("沒有引用的答案", SRC)[0] == "沒有引用的答案")
check("無來源", validate_citations(ans, {})[0] == ans)

print()
if FAIL:
    print(f"共 {FAIL} 項失敗")
    sys.exit(1)
print("全部通過")
