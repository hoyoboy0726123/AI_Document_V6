# -*- coding: utf-8 -*-
"""把 embedding 被清空的 chunk 重新嵌入，並同步更新 FAISS。

為什麼一定要跑：只清空 DB 的 embedding 欄位是不夠的 —— FAISS 索引裡仍然是
「舊文字的向量」，而 chunk 的文字已經改了。這種狀態比沒改還糟：檢索用舊語意
命中，回傳的卻是新內容，兩者不一致且完全看不出來。

add_embeddings 用 IndexIDMap2 的 add_with_ids，同一個 faiss_id 會覆蓋舊向量，
所以先 remove 再 add 才乾淨。

用法：.venv\\Scripts\\python.exe scripts\\reembed_dirty_chunks.py [--dry-run]
"""
from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sqlalchemy import text as sa_text  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.services import ai, vector_store  # noqa: E402

DRY = "--dry-run" in sys.argv
BATCH = 8


def main() -> int:
    db = SessionLocal()
    try:
        rows = db.execute(sa_text(
            "SELECT id, faiss_id, text FROM document_chunks "
            "WHERE embedding IS NULL OR embedding = '[]' OR embedding = ''"
        )).fetchall()
        print(f"待重新嵌入的 chunk：{len(rows)} 塊")
        if not rows or DRY:
            return 0

        done = 0
        for i in range(0, len(rows), BATCH):
            batch = rows[i:i + BATCH]
            vectors = ai.embed_texts([r[2] or "" for r in batch])
            if len(vectors) != len(batch):
                print(f"  ⚠ 批次 {i} 回傳 {len(vectors)} 個向量，預期 {len(batch)}，跳過")
                continue
            payload = {}
            for (cid, fid, _t), vec in zip(batch, vectors):
                db.execute(sa_text("UPDATE document_chunks SET embedding = :e WHERE id = :i"),
                           {"e": str(list(vec)), "i": cid})
                if fid is not None:
                    payload[int(fid)] = list(vec)
            if payload:
                # 先移除再加入：IndexIDMap2 不會自動去重，殘留舊向量會讓同一個
                # chunk 在候選池裡出現兩次（一次舊語意、一次新語意）。
                vector_store.remove_embeddings(list(payload.keys()))
                vector_store.add_embeddings(payload)
            done += len(batch)
            print(f"  已處理 {done}/{len(rows)}")
        db.commit()
        print("完成。")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
