# -*- coding: utf-8 -*-
"""把 kg_headings 抽出的章節標題貼到 document_chunks.section_path。

為什麼獨立一個模組：入庫流程（kg_pipeline）與一次性回填腳本
（scripts/backfill_section_path.py）都要用同一套規則。分成兩份實作的話，
新進來的文件與既有語料會用不同標準貼標籤，而這種不一致只有在很久以後
出現「同一個方法的兩段證據被判成不同方法」時才會被發現。

只用 method / annex，不用 section：實測 41 份文件裡只有 MIL-STD-810H 有
method/annex（30 + 34 筆），而 type='section' 的 2379 筆雜訊很高
（「26 June 2020」「40 in. (1016 mm)」都被當成章節標題）。貼錯的標籤
比沒有標籤更糟 —— 它會讓 in_scope 誤擋掉正確的證據。
"""
from __future__ import annotations

import json
import logging
import re
from bisect import bisect_right
from typing import List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_METHOD_IN_TEXT = re.compile(r"METHOD\s+(\d{3}\.\d)")


def _headings_for(db: Session, document_id: str) -> List[Tuple[int, str]]:
    rows = db.execute(text(
        "SELECT name, meta FROM kg_entities WHERE type IN ('method','annex')"
    )).fetchall()
    heads: List[Tuple[int, str]] = []
    for name, meta in rows:
        try:
            m = json.loads(meta or "{}")
        except (TypeError, ValueError):
            continue
        if m.get("document_id") == document_id and isinstance(m.get("page"), int):
            heads.append((m["page"], name))
    heads.sort()
    return heads


def _resolve(label: str, body: Optional[str], heads: List[Tuple[int, str]], i: int) -> Tuple[str, bool]:
    """邊界修正：標題起始頁與方法實際起始頁常差一頁。

    方法交界處約 10% 的 chunk 會被貼到前一個方法（實測一致率 90.5%）。
    810H 每頁頁尾都印著 "METHOD 509.7"，內文若一致地指向另一個方法、
    且那正是相鄰的方法，就採信內文（修正後 99.2%）。

    限定「相鄰」是為了不被交互參照騙走：509 的段落引用 505 是常態，
    那種情況內文與標籤相差好幾個方法，不會通過這道條件。
    """
    found = set(_METHOD_IN_TEXT.findall(body or ""))
    cur = _METHOD_IN_TEXT.search(label)
    if len(found) != 1 or not cur or cur.group(1) in found:
        return label, False
    only = next(iter(found))
    neighbours = {m.group(1) for h in heads[max(0, i - 1):i + 2]
                  if (m := _METHOD_IN_TEXT.search(h[1]))}
    if only not in neighbours:
        return label, False
    for h in heads:
        m = _METHOD_IN_TEXT.search(h[1])
        if m and m.group(1) == only:
            return h[1], True
    return label, False


def assign_section_paths(db: Session, document_id: str, commit: bool = True) -> int:
    """為一份文件的所有 chunk 貼上章節標籤，回傳貼到的數量。

    沒有 method/annex 標題的文件（41 份裡有 40 份）直接回 0，section_path 留 NULL，
    in_scope 會自動退回原本的頁碼判斷。
    """
    heads = _headings_for(db, document_id)
    if not heads:
        return 0
    pages = [p for p, _ in heads]
    chunks = db.execute(text(
        "SELECT id, page, text FROM document_chunks WHERE document_id = :d"
    ), {"d": document_id}).fetchall()

    updates, fixed = [], 0
    for cid, page, body in chunks:
        if page is None:
            continue
        i = bisect_right(pages, page) - 1
        if i < 0:
            continue            # 第一個 METHOD 之前（Part One）→ 不貼標籤
        label, was_fixed = _resolve(heads[i][1], body, heads, i)
        fixed += was_fixed
        updates.append({"p": label[:200], "i": cid})

    if updates:
        db.execute(text("UPDATE document_chunks SET section_path = :p WHERE id = :i"), updates)
        if commit:
            db.commit()
    logger.info("section_path：%s 貼上 %d/%d 個 chunk（邊界修正 %d）",
                document_id[:8], len(updates), len(chunks), fixed)
    return len(updates)
