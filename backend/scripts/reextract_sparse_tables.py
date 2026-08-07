"""重抽「含被誤判為空白表格」的文件（_sparse_table_text 修正後）。

排除兩類文件，兩類都有前車之鑑：

  1. ocr_method 不是 None 的 → 內容來自 VL/OCR，不是文字層。
     上次用 is_image_based=False 當條件時，教育訓練文件（ocr_method='vision'
     但 is_image_based=False）被當成文字型重抽，10 個區塊被砍成 1 個。
     旗標對不上是既有資料的問題，這裡改用 ocr_method 判斷，不依賴那個旗標。

  2. 文字層全是 (cid:N) 的 → 重抽只會再得到一次亂碼，那些需要 OCR。

用法：
    python scripts/reextract_sparse_tables.py --dry-run
    python scripts/reextract_sparse_tables.py --only MIL-STD-704F
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, ".")

import pdfplumber  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models import Document, DocumentChunk  # noqa: E402
from app.services.documents import DocumentService  # noqa: E402
from app.services.pdf_processing import _is_mostly_empty  # noqa: E402

_CID_RE = re.compile(r"\(cid:\d+\)")


def sparse_table_pages(path: str) -> int:
    try:
        with pdfplumber.open(path) as pdf:
            n = 0
            for pg in pdf.pages:
                for tb in pg.find_tables():
                    d = tb.extract()
                    if d and _is_mostly_empty(d):
                        n += 1
            return n
    except Exception:  # noqa: BLE001
        return 0


def is_cid_garbage(path: str, sample: int = 8) -> bool:
    try:
        with pdfplumber.open(path) as pdf:
            bad = checked = 0
            for pg in pdf.pages[:sample]:
                t = pg.extract_text() or ""
                if not t.strip():
                    continue
                checked += 1
                if sum(len(m) for m in _CID_RE.findall(t)) / len(t) > 0.3:
                    bad += 1
            return checked > 0 and bad == checked
    except Exception:  # noqa: BLE001
        return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--backup-dir", default="")
    args = ap.parse_args()

    db = SessionLocal()
    targets = []
    for d in db.query(Document).filter(Document.pdf_path.isnot(None)).all():
        if not d.pdf_path or not os.path.exists(d.pdf_path):
            continue
        if args.only and args.only.lower() not in (d.title or "").lower():
            continue
        if d.ocr_method:
            print(f"  [排除] {(d.title or '')[:40]}：內容來自 {d.ocr_method}，不是文字層")
            continue
        if is_cid_garbage(d.pdf_path):
            print(f"  [排除] {(d.title or '')[:40]}：文字層全是 cid 亂碼，需 OCR")
            continue
        n = sparse_table_pages(d.pdf_path)
        if n:
            targets.append((d, n))

    print(f"\n待重抽 {len(targets)} 份")
    for d, n in sorted(targets, key=lambda x: -x[1]):
        cnt = db.query(DocumentChunk).filter(DocumentChunk.document_id == d.id).count()
        print(f"  {(d.title or '')[:44]:<46} 稀疏表格 {n:>3} 個　現有區塊 {cnt}")

    if args.dry_run:
        print("\n--dry-run：未做任何變更")
        db.close()
        return

    backup_dir = args.backup_dir or f"backup_sparse_{datetime.now():%Y%m%d_%H%M%S}"
    os.makedirs(backup_dir, exist_ok=True)
    print(f"\n備份目錄：{backup_dir}", flush=True)

    for d, _n in targets:
        rows = db.query(DocumentChunk).filter(DocumentChunk.document_id == d.id).all()
        with open(os.path.join(backup_dir, f"{d.id}.json"), "w", encoding="utf-8") as f:
            json.dump({"document_id": d.id, "title": d.title,
                       "chunks": [{"chunk_index": c.chunk_index, "page": c.page,
                                   "paragraph_index": c.paragraph_index, "text": c.text}
                                  for c in rows]}, f, ensure_ascii=False)
        before = len(rows)
        try:
            DocumentService(db)._rebuild_document_chunks(
                d, Path(d.pdf_path), force_ocr=False, force_vision=False)
            db.commit()
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            print(f"  [失敗] {(d.title or '')[:40]}：{exc}", flush=True)
            continue
        after = db.query(DocumentChunk).filter(DocumentChunk.document_id == d.id).count()
        flag = "  ← 區塊數大幅減少，請檢查備份" if after < before * 0.6 else ""
        print(f"  [完成] {(d.title or '')[:40]:<42} {before} → {after}{flag}", flush=True)

    db.close()
    print("\n完成。備份保留在", backup_dir)


if __name__ == "__main__":
    main()
