"""驗證「以容差分行」能否修好 PDF 文字被打散的問題。只讀，不改系統。

現行 _extract_page_content() 用 round(word["top"], 1) 當分行鍵。
但 ±、上標數字（m3 的 3、ft3 的 3）在 PDF 裡的 top 會比同行一般字高約 1 point，
四捨五入後仍落在不同鍵 → 同一視覺行被拆成多組 → 依 y 排序合併後順序錯亂。

實例（第 271 頁）：
    'at'   top=269.370   '10.6' top=269.370
    '±7'   top=268.504   'g/m3' top=268.269   '(0.3' top=269.338
→ 產出 "g/m3 g/ft3). ±7 (0.3 ±0.2 ... at 10.6"（單位跑到句首、數值留在句尾）

修法：依 top 排序後用容差群聚成行，行內再依 x0 排序。
以 pdfplumber extract_text(layout=True) 的輸出作為對照基準。
"""
from __future__ import annotations

import argparse
import re
import statistics

import pdfplumber

PDF = "storage/documents/ccc3fd3f-6849-488c-a459-5ddc53f7b875_MIL-STD-810H.pdf"

# 檢查點：這些字串在原文是同一行，若分行錯誤就會被拆開
PROBES = [
    (271, re.compile(r"10\.6\s*±\s*7\s*g/m3\s*\(0\.3\s*±\s*0\.2\s*g/ft3\)")),
    (269, re.compile(r"1\.\s*5\s*±1\s*m/s|1\.5\s*±1\s*m/s")),
    (274, re.compile(r"8\.9\s*±1\.3\s*m/s")),
    (255, re.compile(r"5\s*±1\s*percent|5±1")),
]


def current_grouping(page) -> str:
    """複製現行 _extract_page_content 的非表格文字組裝（round(top,1) 當鍵）。"""
    words = page.extract_words(keep_blank_chars=False, use_text_flow=True)
    lines: dict[float, list[str]] = {}
    for w in words:
        lines.setdefault(round(w["top"], 1), []).append(w["text"])
    return "\n".join(" ".join(v) for _, v in sorted(lines.items()))


def tolerant_grouping(page, tol_ratio: float = 0.5) -> str:
    """修正版：依 top 排序後以容差群聚成行，行內依 x0 排序。

    容差取「字高中位數 × tol_ratio」，比固定值更能適應不同字級；
    行距通常遠大於字高，故不會把相鄰兩行併在一起。
    """
    words = page.extract_words(keep_blank_chars=False, use_text_flow=True)
    if not words:
        return ""
    heights = [w["bottom"] - w["top"] for w in words if w["bottom"] > w["top"]]
    tol = max(1.5, statistics.median(heights) * tol_ratio) if heights else 2.0

    words = sorted(words, key=lambda w: (w["top"], w["x0"]))
    lines: list[dict] = []
    for w in words:
        if lines and abs(w["top"] - lines[-1]["ref"]) <= tol:
            lines[-1]["words"].append(w)
        else:
            lines.append({"ref": w["top"], "words": [w]})
    out = []
    for ln in lines:
        ordered = sorted(ln["words"], key=lambda w: w["x0"])
        out.append(" ".join(w["text"] for w in ordered))
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", default=PDF)
    args = ap.parse_args()

    results = []
    with pdfplumber.open(args.pdf) as pdf:
        for page_no, probe in PROBES:
            page = pdf.pages[page_no - 1]
            cur = current_grouping(page)
            fix = tolerant_grouping(page)
            ref = page.extract_text(layout=True) or ""
            row = (page_no, bool(probe.search(cur)), bool(probe.search(fix)), bool(probe.search(ref)))
            results.append(row)
            print(f"第{page_no}頁　現行 {'✅' if row[1] else '❌'}　"
                  f"修正版 {'✅' if row[2] else '❌'}　"
                  f"對照(layout=True) {'✅' if row[3] else '❌'}")
            if not row[1] and row[2]:
                m = probe.search(fix)
                print(f"    修正後: ...{fix[max(0, m.start()-60):m.end()+20]}...")

    print()
    cur_ok = sum(1 for r in results if r[1])
    fix_ok = sum(1 for r in results if r[2])
    ref_ok = sum(1 for r in results if r[3])
    print(f"完整保留數值的頁數：現行 {cur_ok}/{len(results)}　"
          f"修正版 {fix_ok}/{len(results)}　對照 {ref_ok}/{len(results)}")


if __name__ == "__main__":
    main()
