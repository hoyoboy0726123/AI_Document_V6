# -*- coding: utf-8 -*-
"""一次性回填 document_chunks.section_path（既有語料用）。

規則本身在 app/services/section_path.py，入庫流程（kg_pipeline）用的是同一份 ——
腳本只負責「對所有既有文件跑一遍」與驗證輸出。

不需要重新解析 PDF、也不需要重新嵌入：標題連同頁碼早就存在 kg_entities 裡。

用法：.venv\\Scripts\\python.exe scripts\\backfill_section_path.py [--dry-run]
"""
from __future__ import annotations

import re
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.services.section_path import assign_section_paths  # noqa: E402

DRY = "--dry-run" in sys.argv
_R = re.compile(r"METHOD\s+(\d{3}\.\d)")


def main() -> int:
    db = SessionLocal()
    try:
        docs = db.execute(text("SELECT id, title FROM documents")).fetchall()
        total = 0
        for did, title in docs:
            n = assign_section_paths(db, did, commit=False)
            if n:
                print(f"  {(title or '')[:40]:42s} {n} 個 chunk")
                total += n
        if DRY:
            db.rollback()
        else:
            db.commit()
        print(f"\n共回填 {total} 個 chunk{'（dry-run，已回滾）' if DRY else ''}")

        # 自證：標籤是否與內文印的頁尾 METHOD 一致
        ok = bad = 0
        for sp, txt in db.execute(text(
            "SELECT section_path, text FROM document_chunks "
            "WHERE section_path IS NOT NULL AND text LIKE '%METHOD 5%'"
        )):
            f, lab = set(_R.findall(txt)), _R.search(sp)
            if not f or not lab:
                continue
            ok += lab.group(1) in f
            bad += lab.group(1) not in f
        if ok + bad:
            print(f"內文自證一致率 {ok / (ok + bad):.1%}（一致 {ok} / 不一致 {bad}）")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
