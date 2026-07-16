import sqlite3
c = sqlite3.connect("doc_management.db", timeout=15)
DOC = "ccc3fd3f-6849-488c-a459-5ddc53f7b875"

print("entity types:", dict(c.execute("select type,count(1) from kg_entities group by type").fetchall()))
print("rel types:", dict(c.execute("select rel_type,count(1) from kg_relations group by rel_type").fetchall()))

def cid(suffix):
    return f"doc:{DOC}#{suffix}"

for key in ["510.7", "504.3", "504.3/2.2.2", "504.3-A"]:
    row = c.execute("select id,type,name from kg_entities where canonical_id=?", (cid(key),)).fetchone()
    print(f"\n[{cid(key)}] -> {row[1] if row else 'MISSING'} | {row[2][:60] if row else ''}")
    if row:
        # outgoing references (section -> standard)
        refs = c.execute("""select e2.name from kg_relations r join kg_entities e2 on r.dst_id=e2.id
                            where r.src_id=? and r.rel_type='references' order by e2.name""", (row[0],)).fetchall()
        if refs:
            print("   references:", [x[0] for x in refs][:25])
        # children via part_of
        kids = c.execute("""select e1.name from kg_relations r join kg_entities e1 on r.src_id=e1.id
                            where r.dst_id=? and r.rel_type='part_of' limit 8""", (row[0],)).fetchall()
        if kids:
            print("   part_of children (sample):", [x[0][:32] for x in kids])
c.close()
