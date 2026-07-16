"""Sequentially run KG extraction for every MIL/AR doc that has no KG yet.
Sequential = one at a time (wait for each to finish) to avoid SQLite write-lock contention."""
import time, sqlite3, requests

BASE = "http://127.0.0.1:8001/api/v1"

def login():
    for a in range(6):
        r = requests.post(f"{BASE}/auth/login", data={"username": "admin", "password": "Admin@123"})
        if r.status_code == 429: time.sleep(20 + a*10); continue
        r.raise_for_status(); return {"Authorization": f"Bearer {r.json()['access_token']}"}
    raise RuntimeError("login rate-limited")

H = login()

def docs_needing_kg():
    c = sqlite3.connect("doc_management.db")
    # MIL/AR docs that have chunks but no doc:<id># KG entities yet
    rows = c.execute("""
        select d.id, d.title from documents d
        where (d.title like 'MIL-%' or d.title like 'AR %')
          and (select count(1) from document_chunks dc where dc.document_id=d.id) > 0
        order by (select count(1) from document_chunks dc where dc.document_id=d.id) asc
    """).fetchall()
    out = []
    for did, title in rows:
        has = c.execute("select count(1) from kg_entities where canonical_id like ?", (f"doc:{did}#%",)).fetchone()[0]
        if has == 0:
            out.append((did, title))
    c.close()
    return out

def counts():
    c = sqlite3.connect("doc_management.db")
    e = c.execute("select count(1) from kg_entities").fetchone()[0]
    r = c.execute("select count(1) from kg_relations").fetchone()[0]
    c.close(); return e, r

todo = docs_needing_kg()
e0, r0 = counts()
print(f"docs needing KG: {len(todo)} | start entities={e0} relations={r0}\n", flush=True)

for idx, (did, title) in enumerate(todo, 1):
    try:
        rr = requests.post(f"{BASE}/kg/extract/{did}", headers=H, timeout=30)
        if rr.status_code == 401: H = login(); rr = requests.post(f"{BASE}/kg/extract/{did}", headers=H, timeout=30)
    except Exception as e:
        print(f"[{idx}/{len(todo)}] {title}: trigger error {e}", flush=True); continue
    task_id = rr.json().get("task_id") if rr.status_code in (200, 202) else None
    if not task_id:
        print(f"[{idx}/{len(todo)}] {title}: no task ({rr.status_code})", flush=True); continue
    t0 = time.perf_counter()
    while True:
        time.sleep(12)
        c = sqlite3.connect("doc_management.db", timeout=20)
        row = c.execute("select status from background_tasks where id=?", (task_id,)).fetchone()
        c.close()
        el = int(time.perf_counter() - t0)
        if row and row[0] in ("completed", "failed"):
            e1, r1 = counts()
            print(f"[{idx}/{len(todo)}] {title:<18} {row[0]:<9} (+{e1-e0}e/+{r1-r0}r tot {e1}/{r1}) {el}s", flush=True)
            e0, r0 = e1, r1
            break
        if el > 1800:
            print(f"[{idx}/{len(todo)}] {title}: timeout (>1800s)", flush=True); break

ef, rf = counts()
print(f"\n=== KG done: entities {ef} relations {rf} ===", flush=True)
