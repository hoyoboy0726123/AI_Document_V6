"""Clean noisy supersedes edges: for same-family version pairs, ensure direction is
higher-version → lower-version (flip wrong ones, drop self/equal). Cross-family edges
(e.g. MIL-HDBK-310 → MIL-STD-210, which is a legit cross-series supersession) are kept."""
import re, sqlite3

c = sqlite3.connect("doc_management.db")

def base_rank(cid):
    cid = cid or ""
    m = re.search(r"(?<=\d)([A-Z])$", cid)        # trailing version letter after a digit
    if m:
        return cid[:-1], ord(m.group(1)) - ord("A") + 1
    return cid, 0

rows = c.execute("""
    select r.id, e1.canonical_id, e2.canonical_id
    from kg_relations r
    join kg_entities e1 on r.src_id=e1.id
    join kg_entities e2 on r.dst_id=e2.id
    where r.rel_type='supersedes'
""").fetchall()

flip, drop, keep = 0, 0, 0
for rid, src, dst in rows:
    b1, k1 = base_rank(src)
    b2, k2 = base_rank(dst)
    if b1 != b2:
        keep += 1; continue                       # cross-family: leave as-is
    if k1 == k2:
        c.execute("delete from kg_relations where id=?", (rid,)); drop += 1; continue
    if k1 < k2:                                    # wrong direction (lower supersedes higher) → flip
        s = c.execute("select src_id,dst_id from kg_relations where id=?", (rid,)).fetchone()
        c.execute("update kg_relations set src_id=?, dst_id=? where id=?", (s[1], s[0], rid)); flip += 1
    else:
        keep += 1

# dedupe identical (src,dst,rel_type) supersedes edges
c.execute("""
  delete from kg_relations where id not in (
    select min(id) from kg_relations where rel_type='supersedes' group by src_id,dst_id,rel_type
  ) and rel_type='supersedes'
""")
c.commit()
n = c.execute("select count(1) from kg_relations where rel_type='supersedes'").fetchone()[0]
print(f"supersedes edges: flipped={flip} dropped(self)={drop} kept={keep} | total now {n}")
print("\nsame-family examples after fix:")
for s, d in c.execute("""select e1.canonical_id, e2.canonical_id from kg_relations r
   join kg_entities e1 on r.src_id=e1.id join kg_entities e2 on r.dst_id=e2.id
   where r.rel_type='supersedes' and (e1.canonical_id like 'MIL-STD-210%' or e1.canonical_id like 'MIL-STD-704%'
        or e1.canonical_id like 'MIL-STD-810%') order by e1.canonical_id limit 12""").fetchall():
    print(f"   {s} 取代 {d}")
c.close()
