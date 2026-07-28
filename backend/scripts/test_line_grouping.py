"""驗證 _extract_page_content 的容差分行修正。

修正前：round(word["top"], 1) 當分行鍵，± 與上標數字（m3 的 3）top 差約 1 point，
同一視覺行被拆成多組，依 y 排序合併後順序錯亂。

驗證方式：以 pdfplumber 自己的 extract_text(layout=True) 為基準，
檢查那些「數值 + 公差 + 單位」是否留在同一行。
"""
from __future__ import annotations

import re
import sys

sys.path.insert(0, ".")

import pdfplumber  # noqa: E402

from app.services.pdf_processing import _extract_page_content  # noqa: E402

PDF = "storage/documents/ccc3fd3f-6849-488c-a459-5ddc53f7b875_MIL-STD-810H.pdf"

# (頁碼, 應該保持完整的字串樣態, 說明)
CASES = [
    (271, re.compile(r"10\.6\s*±7\s*g/m3\s*\(0\.3\s*±0\.2\s*g/ft3\)"), "塵土濃度（原本被打散）"),
    (269, re.compile(r"±1\s*m/s"), "空氣流速"),
    (274, re.compile(r"8\.9\s*±1\.3\s*m/s"), "高流速段"),
]

results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{('  ' + extra) if extra else ''}")


def main() -> None:
    with pdfplumber.open(PDF) as pdf:
        for page_no, probe, desc in CASES:
            page = pdf.pages[page_no - 1]
            got = _extract_page_content(page)
            ref = page.extract_text(layout=True) or ""
            m = probe.search(got)
            check(f"第{page_no}頁 {desc} 保持同一行", bool(m),
                  (f"→ {m.group(0)}" if m else "(被打散)"))
            # 基準一致性：pdfplumber 自己抓得到的，我們也該抓得到
            if probe.search(ref):
                check(f"第{page_no}頁 與 layout=True 基準一致", bool(m))

        # 不可回歸：一般段落不能因為容差而把相鄰兩行誤併
        page = pdf.pages[270]
        got = _extract_page_content(page)
        # 注意用 [ \t]* 而非 \s*：\s 會匹配換行，正確換行也會被誤判成「誤併」
        merged_wrongly = re.search(r"g/ft3\)\.[ \t]*This concentration", got)
        check("相鄰兩行未被誤併", merged_wrongly is None,
              "（句號後應換行）" if merged_wrongly is None else "(誤併)")

        # 表格處理未受影響：仍能輸出 markdown pipe 表格
        has_table_page = None
        for i in range(250, 290):
            if pdf.pages[i].find_tables():
                has_table_page = i
                break
        if has_table_page is not None:
            out = _extract_page_content(pdf.pages[has_table_page])
            check(f"第{has_table_page+1}頁 表格仍輸出 markdown", "|" in out)

    print()
    print("全部通過 ✅" if all(o for _, o in results) else "有失敗 ❌")


if __name__ == "__main__":
    main()
