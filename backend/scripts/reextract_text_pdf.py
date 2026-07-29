"""以「文字抽取」路徑重跑指定文件（不是 OCR）。

用途：套用容差分行修正後，讓既有文件的文字重新抽取一次。

注意：前端的「高精度重跑」走的是 run_document_reocr_task(force_ocr=True)，
會強制把數位文字 PDF 轉圖片再 OCR（MIL-STD-810H 1089 頁在 CPU 上約 26 小時，
且會引入字元辨識誤差）。這支腳本走 force_ocr=False 的文字路徑，
實測 810H 全書抽取約 1 分鐘。

安全性：_rebuild_document_chunks 採「先建後換」（稽核 C4），
新內容抽取+嵌入成功後才在同一交易內換掉舊 chunk/向量；
且不會動到 DocumentNote / DocumentUserAnalysis（筆記與分析紀錄保留）。
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
    ap.add_argument("--title-like", required=True)
    ap.add_argument("--yes", action="store_true", help="不詢問直接執行")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        doc = db.query(Document).filter(Document.title.like(args.title_like)).first()
        if not doc:
            raise SystemExit(f"找不到符合 {args.title_like} 的文件")

        before = db.query(DocumentChunk).filter(DocumentChunk.document_id == doc.id).count()
        pdf_path = Path(doc.pdf_path)
        print(f"文件　：{doc.title}")
        print(f"PDF 　：{pdf_path}（存在：{pdf_path.exists()}）")
        print(f"目前　：{before} 個 chunk，is_image_based={doc.is_image_based}，"
              f"ocr_status={doc.ocr_status}")
        print("路徑　：force_ocr=False → 文字抽取（非 OCR）")
        if not pdf_path.exists():
            raise SystemExit("原始 PDF 不存在，無法重跑")
        if not args.yes:
            raise SystemExit("加上 --yes 才會實際執行")

        svc = DocumentService(db)
        t0 = time.perf_counter()
        svc._rebuild_document_chunks(doc, pdf_path, force_ocr=False, force_vision=False)
        db.commit()
        dt = time.perf_counter() - t0

        after = db.query(DocumentChunk).filter(DocumentChunk.document_id == doc.id).count()
        print(f"\n完成：{dt/60:.1f} 分鐘")
        print(f"  chunk：{before} → {after}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
