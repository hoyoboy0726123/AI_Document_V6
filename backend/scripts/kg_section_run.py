"""Trigger KG re-extract and watch method/section nodes appear (DB reads, WAL-safe)."""
import time, sqlite3, requests

BASE = "http://127.0.0.1:8001/api/v1"
DOC = "ccc3fd3f-6849-488c-a459-5ddc53f7b875"

def login():
    for _ in range(8):
        try:
            r = requests.post(f"{BASE}/auth/login", data={"username": "admin", "password": "Admin@123"}, timeout=15)
            if r.status_code == 200:
                return r.json()["access_token"]
        except Exception:
            pass
        time.sleep(3)
    raise SystemExit("login failed")

H = {"Authorization": f"Bearer {login()}"}
r = requests.post(f"{BASE}/kg/extract/{DOC}", headers=H, timeout=30)
print("trigger ->", r.status_code, str(r.text)[:160], flush=True)
task_id = r.json().get("task_id")

def snap():
    c = sqlite3.connect("doc_management.db", timeout=15)
    c.execute("PRAGMA busy_timeout=8000")
    by_type = dict(c.execute("select type, count(1) from kg_entities group by type").fetchall())
    rels = dict(c.execute("select rel_type, count(1) from kg_relations group by rel_type").fetchall())
    row = c.execute("select status, progress, message from background_tasks where id=?", (task_id,)).fetchone()
    c.close()
    return by_type, rels, row

t0 = time.perf_counter()
last = None
while True:
    time.sleep(20)
    el = int(time.perf_counter() - t0)
    try:
        by_type, rels, row = snap()
    except Exception as ex:
        print(f"[{el}s] db err {ex}", flush=True); continue
    status = row[0] if row else "?"
    m = (by_type.get("method", 0), by_type.get("section", 0), by_type.get("annex", 0))
    line = (f"[{el:>4}s] {status} {row[2] if row else ''} | method={m[0]} section={m[1]} annex={m[2]} "
            f"| part_of={rels.get('part_of',0)} refs={rels.get('references',0)}")
    if line != last:
        print(line, flush=True); last = line
    if status in ("completed", "failed"):
        print(f"\n=== {status} after {el}s | entities={by_type} ===", flush=True)
        print(f"relations={rels}", flush=True)
        break
