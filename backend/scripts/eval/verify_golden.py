"""檢查 golden set 的每個 gold 值都真的存在於語料，且鑑別度足夠。

沒有這道，評測本身可能是錯的 —— 題目的正解若不在語料裡，任何檢索都會被
判為失敗，而看起來像是系統很差。

用法：python scripts/eval/verify_golden.py
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys

sys.path.insert(0, ".")

GOLDEN = "scripts/eval/golden.jsonl"
_MAX_MATCHING_CHUNKS = 40   # 命中太多塊代表這個值太泛用，當不了鑑別題


def norm(v: str) -> str:
    """比對前正規化：去空白、統一度數符號與上標。"""
    s = re.sub(r"\s+", "", v or "").lower()
    return s.replace("º", "°").replace("³", "3").replace("²", "2")


def main() -> None:
    con = sqlite3.connect("file:doc_management.db?mode=ro", uri=True)
    rows = con.execute(
        "SELECT c.id, d.title, c.page, c.text FROM document_chunks c "
        "JOIN documents d ON d.id = c.document_id"
    ).fetchall()
    normed = [(cid, title, page, norm(text)) for cid, title, page, text in rows]

    items = [json.loads(l) for l in open(GOLDEN, encoding="utf-8") if l.strip()]
    problems = 0
    print(f"golden set：{len(items)} 題（{sum(1 for i in items if i['kind']=='none')} 題為『語料內不存在』）\n")

    for it in items:
        if it["kind"] == "none":
            # 反向檢查：不該存在的題目，不需要 gold
            if it["gold"]:
                print(f"[問題] {it['id']} 標為 none 卻有 gold")
                problems += 1
            continue

        hit_chunks, hit_docs, hit_pages = set(), set(), set()
        for g in it["gold"]:
            gn = norm(g)
            for cid, title, page, text in normed:
                if gn and gn in text:
                    hit_chunks.add(cid)
                    hit_docs.add(title)
                    if page:
                        hit_pages.add(page)

        if not hit_chunks:
            print(f"[錯誤] {it['id']} {it['q'][:26]}：gold {it['gold']} 在語料中找不到")
            problems += 1
            continue
        if len(hit_chunks) > _MAX_MATCHING_CHUNKS:
            print(f"[警告] {it['id']} {it['q'][:26]}：命中 {len(hit_chunks)} 塊，鑑別度不足")
            problems += 1
            continue
        wrong_doc = it.get("doc") and not any(it["doc"] in d for d in hit_docs)
        flag = "  ← 與宣告的文件不符" if wrong_doc else ""
        if wrong_doc:
            problems += 1
        print(f"[OK] {it['id']} 命中 {len(hit_chunks):>2} 塊 / {len(hit_docs)} 份 / 頁 {sorted(hit_pages)[:4]}{flag}")

    print()
    print("golden set 可用 ✅" if problems == 0 else f"有 {problems} 題需要修正 ❌")
    con.close()


if __name__ == "__main__":
    main()
