import sqlite3
c = sqlite3.connect(r"doc_management.db")
def cnt(t):
    try:
        return c.execute(f"select count(1) from {t}").fetchone()[0]
    except Exception as e:
        return f"(n/a: {e})"
print("kg_entities =", cnt("kg_entities"))
print("kg_relations =", cnt("kg_relations"))
print("document_chunks =", cnt("document_chunks"))
# any kg_extract task status?
try:
    rows = c.execute("select task_type,status,progress,message from background_tasks where task_type='kg_extract' order by rowid desc limit 3").fetchall()
    for r in rows:
        print("kg_task:", r)
except Exception as e:
    print("kg_task n/a:", e)
