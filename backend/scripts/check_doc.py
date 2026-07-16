import sqlite3, os
c = sqlite3.connect("doc_management.db")
r = c.execute("select id, pdf_path from documents where id=?",
              ("ccc3fd3f-6849-488c-a459-5ddc53f7b875",)).fetchone()
print("doc:", r[0] if r else None)
print("pdf_path:", r[1] if r else None)
print("exists:", os.path.exists(r[1]) if r and r[1] else False)
print("method/section entities now:",
      c.execute("select count(1) from kg_entities where type in ('method','section','annex')").fetchone()[0])
