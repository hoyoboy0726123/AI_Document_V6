import sqlite3
c = sqlite3.connect("doc_management.db")
DID = "ccc3fd3f-6849-488c-a459-5ddc53f7b875"

# Q13: what does the 810H document node 'contains', by type?
docent = c.execute("select id from kg_entities where canonical_id=?", (f"doc:{DID}",)).fetchone()
print("== 810H document node contains-children by type ==")
if docent:
    rows = c.execute("""
      select e.type, count(1) from kg_relations r
      join kg_entities e on r.dst_id=e.id
      where r.src_id=? and r.rel_type='contains' group by e.type
    """, (docent[0],)).fetchall()
    for t, n in rows: print(f"   {t}: {n}")
    nmeth = c.execute("""select count(1) from kg_relations r join kg_entities e on r.dst_id=e.id
       where r.src_id=? and r.rel_type='contains' and e.type='method'""", (docent[0],)).fetchone()[0]
    print(f"   -> method children: {nmeth}")

# Q16/Q17: do supersedes edges exist at all?
print("\n== supersedes relations in whole KG ==")
n = c.execute("select count(1) from kg_relations where rel_type='supersedes'").fetchone()[0]
print(f"   total supersedes edges: {n}")
for s, d in c.execute("""select e1.canonical_id, e2.canonical_id from kg_relations r
   join kg_entities e1 on r.src_id=e1.id join kg_entities e2 on r.dst_id=e2.id
   where r.rel_type='supersedes' limit 15""").fetchall():
    print(f"     {s} supersedes {d}")

# version family nodes present?
print("\n== MIL-STD-810 / 210 family nodes present ==")
for fam in ("MIL-STD-810", "MIL-STD-210"):
    fams = [r[0] for r in c.execute(
      "select canonical_id from kg_entities where canonical_id like ? and canonical_id not like 'doc:%' order by canonical_id",
      (fam+"%",)).fetchall()]
    print(f"   {fam}: {fams}")

# Q12: MIL-PRF-7808 incoming references
print("\n== who references MIL-PRF-7808 (incoming) ==")
ent = c.execute("select id from kg_entities where canonical_id='MIL-PRF-7808'").fetchone()
if ent:
    for s, in c.execute("""select distinct e.canonical_id from kg_relations r
       join kg_entities e on r.src_id=e.id where r.dst_id=? and r.rel_type='references' limit 15""", (ent[0],)).fetchall():
        print(f"     {s} -> MIL-PRF-7808")
c.close()
