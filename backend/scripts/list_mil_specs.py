import sqlite3
c = sqlite3.connect("doc_management.db")
rows = c.execute(
    "select canonical_id, type from kg_entities "
    "where type in ('MIL_STD','MIL_HDBK','MIL_DTL','MIL_PRF') "
    "and canonical_id not like 'doc:%' order by type, canonical_id"
).fetchall()
print(f"total MIL specs: {len(rows)}\n")
for cid, typ in rows:
    print(f"{typ:<10} {cid}")
