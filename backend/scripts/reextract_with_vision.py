"""以 VL 模型重新抽取指定文件（給「文字層壞掉」的 PDF 用）。

適用情境：PDF 有文字層，但字型缺少 ToUnicode 對照表，抽出來全是 (cid:N)
代碼。這種檔案 pdfminer/pdfplumber 都救不了，只能把頁面當影像重新辨識。
實測 MIL-STD-1366E 的 127 頁全部如此。

選 VL 而非 PaddleOCR 是使用者的決定：規範文件表格多，VL 對版面與表格的
理解較好，且本機是 24GB 顯卡跑得動；代價是佔用 GPU。

用法：
    python scripts/reextract_with_vision.py --title MIL-STD-1366E --dry-run
    python scripts/reextract_with_vision.py --title MIL-STD-1366E --yes
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, ".")

from app.database import SessionLocal  # noqa: E402
from app.models import Document, DocumentChunk  # noqa: E402
from app.services.documents import DocumentService  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", required=True, help="文件標題（可為片段）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--backup-dir", default="")
    args = ap.parse_args()

    db = SessionLocal()
    docs = [d for d in db.query(Document).all()
            if args.title.lower() in (d.title or "").lower()]
    if not docs:
        raise SystemExit(f"找不到標題含「{args.title}」的文件")

    for d in docs:
        cnt = db.query(DocumentChunk).filter(DocumentChunk.document_id == d.id).count()
        print(f"目標：{d.title}　現有區塊 {cnt}　路徑 {d.pdf_path}")

    if args.dry_run:
        print("--dry-run：未做任何變更")
        db.close()
        return
    if not args.yes:
        raise SystemExit("加上 --yes 才會實際執行")

    backup_dir = args.backup_dir or f"backup_vision_{datetime.now():%Y%m%d_%H%M%S}"
    os.makedirs(backup_dir, exist_ok=True)
    print(f"備份目錄：{backup_dir}", flush=True)

    for d in docs:
        rows = db.query(DocumentChunk).filter(DocumentChunk.document_id == d.id).all()
        with open(os.path.join(backup_dir, f"{d.id}.json"), "w", encoding="utf-8") as f:
            json.dump({"document_id": d.id, "title": d.title,
                       "chunks": [{"chunk_index": c.chunk_index, "page": c.page,
                                   "paragraph_index": c.paragraph_index, "text": c.text}
                                  for c in rows]}, f, ensure_ascii=False)
        before = len(rows)
        t0 = time.perf_counter()
        try:
            DocumentService(db)._rebuild_document_chunks(
                d, Path(d.pdf_path), force_vision=True, force_ocr=False)
            db.commit()
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            print(f"[失敗] {d.title}：{exc}", flush=True)
            continue
        after = db.query(DocumentChunk).filter(DocumentChunk.document_id == d.id).count()
        print(f"[完成] {d.title}　區塊 {before} → {after}　"
              f"{time.perf_counter()-t0:.0f}s", flush=True)

    db.close()
    print("完成。備份保留在", backup_dir)


if __name__ == "__main__":
    main()
