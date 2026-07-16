import sqlite3
c = sqlite3.connect("doc_management.db", timeout=15)
print("entities:", c.execute("select count(1) from kg_entities").fetchone()[0])
print("relations:", c.execute("select count(1) from kg_relations").fetchone()[0])
print("\n-- entity types --")
for row in c.execute("select type, count(1) n from kg_entities group by type order by n desc"):
    print(f"  {str(row[0]):<10} {row[1]}")
print("\n-- relation types --")
for row in c.execute("select rel_type, count(1) n from kg_relations group by rel_type order by n desc"):
    print(f"  {str(row[0]):<14} {row[1]}")
print("\n-- supersedes / requires / derives_from edges --")
q = """select e1.name, r.rel_type, e2.name, round(r.confidence,2)
       from kg_relations r join kg_entities e1 on r.src_id=e1.id join kg_entities e2 on r.dst_id=e2.id
       where r.rel_type in ('supersedes','requires','derives_from') limit 15"""
for row in c.execute(q):
    print(f"  {row[0]}  --{row[1]}-->  {row[2]}  (conf {row[3]})")
print("\n-- top referenced specs (most incoming 'references') --")
q2 = """select e2.name, count(1) n from kg_relations r join kg_entities e2 on r.dst_id=e2.id
        where r.rel_type='references' group by e2.name order by n desc limit 10"""
for row in c.execute(q2):
    print(f"  {row[0]:<24} <- {row[1]} refs")
c.close()
