import sqlite3
c = sqlite3.connect("doc_management.db")
rows = c.execute("""
  select d.id, d.title, d.ocr_status,
         (select count(1) from document_chunks dc where dc.document_id=d.id) as chunks
  from documents d
  where d.title like 'MIL-%' or d.title like 'AR %' or d.title like '%MIL-HDBK%'
  order by d.title
""").fetchall()
print(f"{'TITLE':<34} {'chunks':>7}  status")
low = []
for did, title, st, n in rows:
    flag = "  <-- LOW/EMPTY" if (n is None or n < 3) else ""
    if n is None or n < 3: low.append((title, did))
    print(f"{title:<34} {str(n):>7}  {st or ''}{flag}")
print(f"\ntotal MIL/AR docs: {len(rows)}")
if low:
    print("\nLOW/EMPTY (likely scanned or incomplete):")
    for t, d in low: print(f"  {t}  id={d}")
c.close()
