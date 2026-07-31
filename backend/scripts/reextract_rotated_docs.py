"""重新抽取「含旋轉文字」的 PDF，並重建其向量。

為什麼是獨立腳本而不沿用 reextract_all_text_pdfs.py：
後者以 is_image_based=False 當條件，會掃到所有文字型文件。上次跑它時，
一份內容其實來自 VL 的教育訓練文件因為旗標是 False 也被重抽，10 個區塊
被砍成 1 個。這支只碰「pdfplumber 真的讀得到旋轉字元」的文件，並且
先把原始區塊備份到 JSON，出事可以還原。

用法：
    python scripts/reextract_rotated_docs.py --dry-run     # 只列出會處理誰
    python scripts/reextract_rotated_docs.py --backup-dir <路徑>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, ".")

import pdfplumber  # noqa: E402

from pathlib import Path  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models import Document, DocumentChunk  # noqa: E402
from app.services.documents import DocumentService  # noqa: E402

_MIN_ROTATED_CHARS = 20
_CID_RE = re.compile(r"\(cid:\d+\)")


def is_cid_garbage(path: str, sample_pages: int = 8) -> bool:
    """文字層是否根本壞掉（幾乎全是 (cid:N) 代碼）。

    這種文件重抽文字只會再得到一次亂碼 —— 它需要的是 OCR，不是本腳本。
    實測 MIL-STD-1366E 的 127 頁全部如此（字型缺 ToUnicode 對照表）。
    """
    try:
        with pdfplumber.open(path) as pdf:
            pages = pdf.pages[:sample_pages]
            bad = 0
            checked = 0
            for pg in pages:
                t = pg.extract_text() or ""
                if not t.strip():
                    continue
                checked += 1
                if sum(len(m) for m in _CID_RE.findall(t)) / len(t) > 0.3:
                    bad += 1
            return checked > 0 and bad == checked
    except Exception:  # noqa: BLE001
        return False


def has_rotated_text(path: str) -> int:
    """回傳「含明顯旋轉文字」的頁數。門檻 20 個字元：少數幾個多半是符號，不值得重抽。"""
    try:
        with pdfplumber.open(path) as pdf:
            return sum(
                1 for pg in pdf.pages
                if sum(1 for c in pg.chars if not c.get("upright", True)) > _MIN_ROTATED_CHARS
            )
    except Exception as e:  # noqa: BLE001
        print(f"    無法開啟：{e}")
        return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--backup-dir", default="")
    ap.add_argument("--only", default="", help="只處理標題含此字串的文件（一次一份，避免長時間累積）")
    ap.add_argument("--list-only", action="store_true", help="只印出待處理標題，一行一個")
    args = ap.parse_args()

    db = SessionLocal()
    targets = []
    for d in db.query(Document).filter(Document.pdf_path.isnot(None)).all():
        if not d.pdf_path or not os.path.exists(d.pdf_path):
            continue
        n = has_rotated_text(d.pdf_path)
        if not n:
            continue
        if is_cid_garbage(d.pdf_path):
            print(f"  [排除] {(d.title or '')[:40]}：文字層全是 cid 亂碼，需 OCR 而非文字重抽")
            continue
        if args.only and args.only.lower() not in (d.title or "").lower():
            continue
        targets.append((d, n))

    if args.list_only:
        for d, _ in targets:
            print(d.title)
        db.close()
        return

    print(f"含旋轉文字的文件：{len(targets)} 份")
    for d, n in sorted(targets, key=lambda x: -x[1]):
        cnt = db.query(DocumentChunk).filter(DocumentChunk.document_id == d.id).count()
        print(f"  {(d.title or '')[:44]:<46} 旋轉頁 {n:>3}  現有區塊 {cnt}")

    if args.dry_run:
        print("\n--dry-run：未做任何變更")
        db.close()
        return

    backup_dir = args.backup_dir or f"backup_rotated_{datetime.now():%Y%m%d_%H%M%S}"
    os.makedirs(backup_dir, exist_ok=True)
    print(f"\n備份目錄：{backup_dir}")

    for d, _n in targets:
        rows = db.query(DocumentChunk).filter(DocumentChunk.document_id == d.id).all()
        with open(os.path.join(backup_dir, f"{d.id}.json"), "w", encoding="utf-8") as f:
            json.dump(
                {"document_id": d.id, "title": d.title,
                 "chunks": [{"chunk_index": c.chunk_index, "page": c.page,
                             "paragraph_index": c.paragraph_index, "text": c.text}
                            for c in rows]},
                f, ensure_ascii=False,
            )
        before = len(rows)

        try:
            svc = DocumentService(db)
            svc._rebuild_document_chunks(d, Path(d.pdf_path),
                                         force_ocr=False, force_vision=False)
            db.commit()
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            print(f"  [失敗] {(d.title or '')[:40]}：{exc}")
            continue
        after = db.query(DocumentChunk).filter(DocumentChunk.document_id == d.id).count()
        flag = "  ← 區塊數大幅減少，請檢查備份" if after < before * 0.6 else ""
        print(f"  [完成] {(d.title or '')[:40]:<42} 區塊 {before} → {after}{flag}", flush=True)

    db.close()
    print("\n完成。備份保留在", backup_dir)


if __name__ == "__main__":
    main()
