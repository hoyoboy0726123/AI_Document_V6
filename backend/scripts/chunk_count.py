import sqlite3
c = sqlite3.connect(r"doc_management.db")
total = c.execute("select count(1) from document_chunks").fetchone()[0]
doc = c.execute("select count(1) from document_chunks where document_id=?",
                ("ccc3fd3f-6849-488c-a459-5ddc53f7b875",)).fetchone()[0]
print("total_chunks=", total, " doc_chunks=", doc)
