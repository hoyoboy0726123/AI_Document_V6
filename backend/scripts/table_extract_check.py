"""小規模驗證：第 271 頁的塵土濃度數值，各種抽取方式能不能保住「10.6 ±7 g/m3」。

只讀原始 PDF，不寫入資料庫、不動任何既有資料。

比對對象：
  0. 資料庫現況（目前 chunk 裡的樣子）
  1. 專案現行管線 extract_text_and_segments（pdfplumber find_tables + layout 文字）
  2. pdfplumber find_tables（看這一頁到底有沒有被判定為表格）
  3. pdfplumber extract_text(layout=True)
  4. PyMuPDF find_tables（另一套表格偵測，作為對照）
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys

sys.path.insert(0, ".")

TARGET = re.compile(r"10\.6\s*±?\s*7|10\.6.{0,40}g/m", re.I | re.S)


def show(name: str, text: str) -> None:
    ok = bool(TARGET.search(text or ""))
    print(f"\n--- {name} ---")
    print(f"  含完整「10.6 ±7 g/m3」: {'✅ 是' if ok else '❌ 否'}")
    i = (text or "").find("10.6")
    if i >= 0:
        print("  上下文:", repr(text[max(0, i - 90):i + 90]))
    else:
        j = (text or "").lower().find("concentration")
        if j >= 0:
            print("  concentration 附近:", repr(text[max(0, j - 60):j + 120]))
        else:
            print("  (找不到 10.6 或 concentration)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="doc_management.db")
    ap.add_argument("--page", type=int, default=271)
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    row = con.execute(
        "select title, pdf_path from documents where title like '%810H%' limit 1"
    ).fetchone()
    title, pdf_path = row
    dbtext = con.execute(
        """select ch.text from document_chunks ch join documents d on d.id = ch.document_id
           where d.title like '%810H%' and ch.page = ? limit 1""",
        (args.page,),
    ).fetchone()
    con.close()

    print(f"文件：{title}")
    print(f"PDF ：{pdf_path}")
    print(f"檢查頁：{args.page}")

    show("0. 資料庫現況", dbtext[0] if dbtext else "")

    # 1) pdfplumber：這一頁有沒有被判定成表格
    try:
        import pdfplumber

        with pdfplumber.open(pdf_path) as pdf:
            page = pdf.pages[args.page - 1]
            tables = page.find_tables()
            print(f"\n--- 1. pdfplumber find_tables ---")
            print(f"  偵測到表格數：{len(tables)}")
            for ti, t in enumerate(tables[:3], 1):
                data = t.extract()
                flat = " | ".join(str(c) for r in data[:3] for c in r if c)
                print(f"    表{ti}: {len(data)} 列  預覽: {flat[:110]}")
            show("2. pdfplumber extract_text(layout=True)", page.extract_text(layout=True) or "")
    except Exception as exc:  # noqa: BLE001
        print(f"  pdfplumber 失敗：{exc}")

    # 3) PyMuPDF 的表格偵測（另一套演算法，對無框線表格較寬容）
    try:
        import fitz

        doc = fitz.open(pdf_path)
        page = doc[args.page - 1]
        tf = page.find_tables()
        print(f"\n--- 3. PyMuPDF find_tables ---")
        print(f"  偵測到表格數：{len(tf.tables)}")
        for ti, t in enumerate(tf.tables[:3], 1):
            data = t.extract()
            flat = " | ".join(str(c) for r in data[:3] for c in r if c)
            print(f"    表{ti}: {len(data)} 列  預覽: {flat[:110]}")
        show("4. PyMuPDF get_text()", page.get_text())
        doc.close()
    except Exception as exc:  # noqa: BLE001
        print(f"  PyMuPDF 失敗：{exc}")

    # 5) 專案現行管線（單頁）
    try:
        from app.services.pdf_processing import extract_text_and_segments

        text, segments = extract_text_and_segments(pdf_path)
        page_text = "\n".join(
            (s.get("text") if isinstance(s, dict) else s.text)
            for s in segments
            if (s.get("page") if isinstance(s, dict) else s.page) == args.page
        )
        show("5. 專案現行管線 extract_text_and_segments", page_text)
    except Exception as exc:  # noqa: BLE001
        print(f"\n  專案管線失敗：{exc}")


if __name__ == "__main__":
    main()
