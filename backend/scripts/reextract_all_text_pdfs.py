"""批次以文字路徑重跑所有文字型 PDF（套用容差分行修正）。

只處理 is_image_based=False 的文件：圖片型文件走 OCR 路徑，不受本次修正影響，
且用 force_ocr=False 重跑會抽不到文字。

安全性：_rebuild_document_chunks 採「先建後換」，單份失敗不影響其他文件，
也不會動到 DocumentNote / DocumentUserAnalysis。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")

from app.database import SessionLocal  # noqa: E402
from app.models import Document, DocumentChunk  # noqa: E402
from app.services.documents import DocumentService  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--skip", nargs="*", default=[], help="標題含這些關鍵字的文件跳過")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        docs = (
            db.query(Document)
            .filter(Document.is_image_based == False)  # noqa: E712
            .order_by(Document.title)
            .all()
        )
        targets = []
        for d in docs:
            if any(s.lower() in (d.title or "").lower() for s in args.skip):
                continue
            if not d.pdf_path or not Path(d.pdf_path).exists():
                print(f"[跳過] 原始 PDF 不存在：{d.title[:44]}")
                continue
            targets.append(d)

        total_chunks = sum(
            db.query(DocumentChunk).filter(DocumentChunk.document_id == d.id).count()
            for d in targets
        )
        print(f"待處理 {len(targets)} 份文件，共 {total_chunks:,} 個 chunk")
        if not args.yes:
            raise SystemExit("加上 --yes 才會實際執行")

        svc = DocumentService(db)
        ok = failed = 0
        t_all = time.perf_counter()
        for i, doc in enumerate(targets, 1):
            before = db.query(DocumentChunk).filter(DocumentChunk.document_id == doc.id).count()
            t0 = time.perf_counter()
            try:
                svc._rebuild_document_chunks(doc, Path(doc.pdf_path), force_ocr=False, force_vision=False)
                db.commit()
                after = db.query(DocumentChunk).filter(DocumentChunk.document_id == doc.id).count()
                ok += 1
                print(f"[{i:2}/{len(targets)}] ✅ {doc.title[:40]:42} "
                      f"{before:5} → {after:5} 塊　{time.perf_counter()-t0:5.1f}s")
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                failed += 1
                print(f"[{i:2}/{len(targets)}] ❌ {doc.title[:40]:42} 失敗：{exc}")

        print(f"\n完成：成功 {ok}　失敗 {failed}　總耗時 {(time.perf_counter()-t_all)/60:.1f} 分鐘")
    finally:
        db.close()


if __name__ == "__main__":
    main()
