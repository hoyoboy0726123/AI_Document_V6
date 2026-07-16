"""Watch KG extraction by reading SQLite directly (WAL reads don't block on the KG writer)."""
import time, sqlite3

TASK = "134728b8-e57f-4638-b4c7-cd6d9e0b99ce"

def snap():
    c = sqlite3.connect("doc_management.db", timeout=15)
    c.execute("PRAGMA busy_timeout=8000")
    ent = c.execute("select count(1) from kg_entities").fetchone()[0]
    rel = c.execute("select count(1) from kg_relations").fetchone()[0]
    row = c.execute("select status, progress, message from background_tasks where id=?", (TASK,)).fetchone()
    c.close()
    return ent, rel, row

t0 = time.perf_counter()
last = None
while True:
    el = int(time.perf_counter() - t0)
    try:
        ent, rel, row = snap()
    except Exception as ex:
        print(f"[{el}s] db err: {ex}", flush=True); time.sleep(30); continue
    status, prog, msg = (row if row else ("?", "?", "?"))
    line = f"[{el:>5}s] {status} {prog}% {msg} | entities={ent} relations={rel}"
    if line != last:
        print(line, flush=True); last = line
    if status in ("completed", "failed"):
        print(f"\n=== kg_extract {status} after {el}s | entities={ent} relations={rel} ===", flush=True)
        break
    time.sleep(30)
