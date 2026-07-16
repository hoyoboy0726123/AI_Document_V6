import sqlite3
c = sqlite3.connect("doc_management.db", timeout=15)
DOC = "ccc3fd3f-6849-488c-a459-5ddc53f7b875"
PREFIX = f"doc:{DOC}#510.7"

# method node + all descendant sections (scope keys are prefixed -> one LIKE pulls the whole subtree)
rows = c.execute(
    "select canonical_id, type, name, id from kg_entities "
    "where canonical_id = ? or canonical_id like ? order by canonical_id",
    (PREFIX, PREFIX + "/%")
).fetchall()
print(f"Method 510.7 subtree: {len(rows)} nodes\n")

def refs(eid):
    return [r[0] for r in c.execute(
        "select e2.canonical_id from kg_relations r join kg_entities e2 on r.dst_id=e2.id "
        "where r.src_id=? and r.rel_type='references' and e2.type not like 'doc%' "
        "and e2.type not in ('section','method','annex','document') order by e2.canonical_id", (eid,))]

total_refs = set()
for cid, typ, name, eid in rows:
    local = cid.replace(PREFIX, "510.7").replace(f"doc:{DOC}#", "")
    rf = refs(eid)
    if rf:
        total_refs.update(rf)
    mark = f"   ──references──> {rf}" if rf else ""
    print(f"  [{typ:<7}] {local:<14} | {name[:46]}{mark}")

print(f"\n== Method 510.7 全章節引用到的外部規範（去重） ==\n  {sorted(total_refs) or '（無外部規範，主要引用內部章節）'}")
c.close()
