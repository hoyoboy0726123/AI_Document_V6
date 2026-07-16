import sqlite3
c = sqlite3.connect("doc_management.db")
DID = "f71a908f-d987-4d33-b5be-8cfe84b52b46"   # MIL-HDBK-310 doc

# the MIL-HDBK-310 spec node + the doc node
print("== nodes named MIL-HDBK-310 ==")
for r in c.execute("select id, canonical_id, type from kg_entities where canonical_id like 'MIL-HDBK-310%' or canonical_id like ?", (f"doc:{DID}%",)).fetchall()[:8]:
    print(" ", r[1], f"({r[2]})", r[0][:8])

# references out of this doc / its sections (what MIL-HDBK-310 cites)
print("\n== references FROM MIL-HDBK-310 doc/sections (outgoing) ==")
q = """select distinct e2.canonical_id, e2.type from kg_relations r
       join kg_entities e1 on r.src_id=e1.id
       join kg_entities e2 on r.dst_id=e2.id
       where e1.canonical_id like ? and r.rel_type='references'
       and e2.type not in ('section','method','annex','document') order by e2.canonical_id"""
rows = c.execute(q, (f"doc:{DID}%",)).fetchall()
for cid, typ in rows:
    print(f"   -> {cid} ({typ})")
print(f"   total: {len(rows)}")

# what the MIL-HDBK-310 SPEC node connects to (incoming: who references it)
print("\n== who references the MIL-HDBK-310 spec node (incoming) ==")
q2 = """select distinct e1.canonical_id from kg_relations r
        join kg_entities e1 on r.src_id=e1.id join kg_entities e2 on r.dst_id=e2.id
        where e2.canonical_id='MIL-HDBK-310' and r.rel_type='references' limit 10"""
for (cid,) in c.execute(q2).fetchall():
    print(f"   {cid} -> MIL-HDBK-310")
c.close()
