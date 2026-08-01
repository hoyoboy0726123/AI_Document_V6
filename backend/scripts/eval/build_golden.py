"""從語料挖出評測題候選（ground truth 由語料本身決定，不靠人工判讀）。

設計取捨：
  ground truth 不寫「哪一頁是對的」，而是寫「答案裡必須出現的數值字串」。
  理由是同一個數值常散落在多個頁（正文、表格、附錄各講一次），指定頁碼會
  讓評測變成在考「有沒有撈到我挑的那一頁」而不是「有沒有找到答案」。
  改用數值字串後，ground truth 集合可由程式重新推導，換了分塊策略也不會失效。

輸出是「候選」而非成品：題目文字仍需人工潤飾成自然問法，並剔除語意不清的。
用法：
    python scripts/eval/build_golden.py --out scripts/eval/candidates.jsonl
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, ".")

# 帶單位的數值 —— 與 agent._VALUE_TOKEN_RE 同源，但這裡要求「有小數或正負公差」，
# 挑出的才是規範真正的限值，而不是「6 hours」這種到處都有的泛用數字。
_DISTINCTIVE = re.compile(
    r"\d+\.\d+\s*(?:±\s*\d+(?:\.\d+)?\s*)?"
    r"(?:%|°\s?[CF]|º[CF]|℃|g/m3|g/m³|g/ft3|m/s|kPa|psi|Hz|mm|cm|ml/cm|V\b|A\b|N\b|g\b)"
    r"|\d+\s*±\s*\d+(?:\.\d+)?\s*"
    r"(?:%|°\s?[CF]|º[CF]|℃|g/m3|g/m³|g/ft3|m/s|kPa|psi|Hz|mm|cm|V\b|A\b|N\b|g\b)"
)

# 方法標題（用來標示題目屬於哪個測試項目）
_METHOD_RE = re.compile(r"METHOD\s+(\d{3}\.\d)", re.I)


def norm(v: str) -> str:
    return re.sub(r"\s+", "", v).lower().replace("º", "°").replace("³", "3")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="doc_management.db")
    ap.add_argument("--out", default="scripts/eval/candidates.jsonl")
    ap.add_argument("--min-docs", type=int, default=1)
    ap.add_argument("--max-chunks", type=int, default=6,
                    help="數值出現在超過這麼多塊就捨棄（太泛用，當不了鑑別題）")
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    cur = con.cursor()
    rows = cur.execute(
        "SELECT c.id, c.document_id, d.title, c.page, c.text "
        "FROM document_chunks c JOIN documents d ON d.id = c.document_id"
    ).fetchall()

    # 數值 → 出現它的所有區塊
    by_value: dict[str, list] = defaultdict(list)
    for cid, did, title, page, text in rows:
        if not text:
            continue
        for m in set(_DISTINCTIVE.findall(text)):
            by_value[norm(m)].append((cid, did, title, page, text, m))

    out = []
    for key, hits in by_value.items():
        if not (args.min_docs <= len({h[1] for h in hits}) and len(hits) <= args.max_chunks):
            continue
        cid, did, title, page, text, raw = hits[0]
        method = _METHOD_RE.search(text)
        # 取數值前後文當「這個數值在講什麼」的線索，供人工寫題目
        i = text.find(raw)
        ctx = re.sub(r"\s+", " ", text[max(0, i - 160):i + 80]).strip()
        out.append({
            "value": raw.strip(),
            "value_norm": key,
            "doc_title": title,
            "method": method.group(1) if method else None,
            "pages": sorted({h[3] for h in hits if h[3]}),
            "n_chunks": len(hits),
            "context": ctx,
        })

    out.sort(key=lambda r: (r["doc_title"], r["method"] or "", r["pages"][:1]))
    with open(args.out, "w", encoding="utf-8") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"候選 {len(out)} 條 → {args.out}")
    per_doc: dict[str, int] = defaultdict(int)
    for r in out:
        per_doc[r["doc_title"]] += 1
    for t, n in sorted(per_doc.items(), key=lambda x: -x[1])[:12]:
        print(f"  {t[:44]:<46} {n}")
    con.close()


if __name__ == "__main__":
    main()
