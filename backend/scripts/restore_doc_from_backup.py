"""從備份資料庫還原指定文件的 chunk 內容並重新嵌入。

用途：批次重跑時，內容來自 VL 分析的文件（PDF 本身幾乎沒有可抽取文字）
若被誤走「文字抽取」路徑，會只剩殘缺內容。此腳本從備份取回原始文字。

作法：把備份的 chunk 文字去除切塊重疊後，當成 segments 交回
_rebuild_document_chunks，由它重新切塊 + 嵌入 + 原子換入。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, ".")

from app.database import SessionLocal  # noqa: E402
from app.models import Document, DocumentChunk  # noqa: E402
from app.services.documents import DocumentService  # noqa: E402

MAX_OVERLAP = 400


def strip_overlap(prev: str, cur: str) -> str:
    """切塊時下一塊會帶前一塊尾端的重疊，直接串接會重複，這裡去掉。"""
    limit = min(len(prev), len(cur), MAX_OVERLAP)
    for n in range(limit, 0, -1):
        if prev[-n:] == cur[:n]:
            return cur[n:]
    return cur


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backup-db", required=True)
    ap.add_argument("--title-like", required=True)
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    bk = sqlite3.connect(args.backup_db)
    rows = bk.execute(
        """select ch.chunk_index, ch.page, ch.paragraph_index, ch.text
           from document_chunks ch join documents d on d.id = ch.document_id
           where d.title like ? order by ch.chunk_index""",
        (args.title_like,),
    ).fetchall()
    bk.close()
    if not rows:
        raise SystemExit("備份中找不到該文件的 chunk")

    # 還原成「無重疊」的段落，交給既有切塊邏輯重新處理
    segments = []
    prev_text = ""
    for _, page, para, text in rows:
        cleaned = strip_overlap(prev_text, text) if prev_text else text
        prev_text = text
        if cleaned.strip():
            segments.append({"page": page, "paragraph_index": para, "text": cleaned})

    total = sum(len(s["text"]) for s in segments)
    print(f"備份 chunk：{len(rows)} 個")
    print(f"還原段落：{len(segments)} 段，共 {total:,} 字（已去除重疊）")

    db = SessionLocal()
    try:
        doc = db.query(Document).filter(Document.title.like(args.title_like)).first()
        if not doc:
            raise SystemExit("目前資料庫找不到該文件")
        before = db.query(DocumentChunk).filter(DocumentChunk.document_id == doc.id).count()
        print(f"目前 chunk：{before}")
        if not args.yes:
            raise SystemExit("加上 --yes 才會實際執行")

        DocumentService(db)._rebuild_document_chunks(
            doc, Path(doc.pdf_path), segments=segments
        )
        db.commit()
        after = db.query(DocumentChunk).filter(DocumentChunk.document_id == doc.id).count()
        print(f"還原完成：{before} → {after} 塊")
    finally:
        db.close()


if __name__ == "__main__":
    main()
