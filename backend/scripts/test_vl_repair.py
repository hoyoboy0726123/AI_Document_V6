# -*- coding: utf-8 -*-
"""驗證 VL 輸出的兩道清理：prompt 範例回聲、形近字校正。

形近字校正的風險是「把兩個真的不同的料號改成同一個」，所以測試重點在
「原生文字層沒有依據時絕不動手」。
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.services.ai import _strip_prompt_echo, repair_code_tokens  # noqa: E402

results = []


def check(name, ok, extra=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{('  ' + extra) if extra else ''}")


# --- prompt 範例回聲 ---
echoed = """## 原物料介紹
| 項目 | 條件A | 條件B |
| --- | --- | --- |
| 數值 | 100 | 200 |
| 碼數 | 1 | 2 |
"""
out = _strip_prompt_echo(echoed)
check("濾掉 prompt 的範例表格列", "條件A" not in out and "| 數值 | 100 | 200 |" not in out)
check("保留真實的表格內容", "| 碼數 | 1 | 2 |" in out and "原物料介紹" in out)
check("新式佔位符也濾掉", "<col1>" not in _strip_prompt_echo("| <col1> | <col2> |\n內文"))
check("不動一般內文", _strip_prompt_echo("這是一段內文\n沒有表格") == "這是一段內文\n沒有表格")

# --- 形近字校正 ---
native = "類別代號 22SW0 FACILITY (SOFTWARE) 08G2000174J3 MAIN_BD"
vl = "| 22SWO | FACILITY (SOFTWARE) |\n| 08G2000174J3 | MAIN_BD |"
fixed, n = repair_code_tokens(vl, native)
check("依原生文字層修正 22SWO → 22SW0", "22SW0" in fixed and "22SWO" not in fixed, f"修正 {n} 處")
check("原本就正確的料號不動", "08G2000174J3" in fixed)

# 不誤改：原生層沒有這個 token 的變體時，一律不動
fixed2, n2 = repair_code_tokens("| ABC123O | x |", "完全無關的原生文字")
check("原生層無依據時不動手", fixed2 == "| ABC123O | x |" and n2 == 0)

# 不誤改：兩個都真實存在的相似料號，不可互相覆蓋
native3 = "料號 A1B2C3 與 A1B2C3 無關的另一個 AIB2C3"
fixed3, n3 = repair_code_tokens("清單：A1B2C3 與 AIB2C3", native3)
check("原生層兩個變體都存在時不動手", "A1B2C3" in fixed3 and "AIB2C3" in fixed3, f"修正 {n3} 處")

check("空輸入安全", repair_code_tokens("", "x") == ("", 0) and repair_code_tokens("x", "") == ("x", 0))
check("純中文不觸發", repair_code_tokens("這是中文內容", "這是中文內容")[1] == 0)

print()
print("全部通過 ✅" if all(results) else f"有 {results.count(False)} 項失敗 ❌")
sys.exit(0 if all(results) else 1)
