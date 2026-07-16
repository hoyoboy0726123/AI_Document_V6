import sqlite3
c = sqlite3.connect("doc_management.db")
rows = c.execute("""
  select d.id, d.title,
    (select count(1) from document_chunks dc where dc.document_id=d.id) chunks,
    (select count(1) from kg_entities ke where ke.canonical_id like 'doc:'||d.id||'#%') kgnodes
  from documents d
  where d.title like 'MIL-%' or d.title like 'AR %'
  order by d.title
""").fetchall()
empty = [t for _,t,n,_ in rows if (n or 0) < 3]
nokg = [t for _,t,n,k in rows if (k or 0) == 0]
print(f"total MIL/AR docs: {len(rows)}")
print(f"docs with <3 chunks: {len(empty)} {empty}")
print(f"docs with 0 KG nodes: {len(nokg)} {nokg}")
te = c.execute("select count(1) from kg_entities").fetchone()[0]
tr = c.execute("select count(1) from kg_relations").fetchone()[0]
tc = c.execute("select count(1) from document_chunks").fetchone()[0]
print(f"\nGLOBAL: entities={te} relations={tr} total_chunks={tc}")
c.close()
