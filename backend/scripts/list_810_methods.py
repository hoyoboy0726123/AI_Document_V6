import sqlite3, re
c = sqlite3.connect("doc_management.db")
DID = "ccc3fd3f-6849-488c-a459-5ddc53f7b875"
rows = c.execute(
    "select canonical_id, name from kg_entities where type='method' and canonical_id like ? order by canonical_id",
    (f"doc:{DID}#%",)).fetchall()
seen = set()
for cid, name in rows:
    key = cid.split("#",1)[1]
    if "/" in key:  # only top-level METHOD nodes, not sub-sections
        continue
    if key in seen: continue
    seen.add(key)
    print(f"  {key:<8} {name}")
print(f"\ntop-level methods: {len(seen)}")
c.close()
