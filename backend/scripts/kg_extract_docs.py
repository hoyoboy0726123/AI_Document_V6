"""Manually trigger KG extraction for the newly-ingested docs and watch counts."""
import time, sqlite3, requests

BASE = "http://127.0.0.1:8001/api/v1"
DOCS = {
    "MIL-HDBK-310": "f71a908f-d987-4d33-b5be-8cfe84b52b46",
    "AR 70-38":     "f8e701ce-50a1-48f3-9ced-2d146b4763fd",
}

r = requests.post(f"{BASE}/auth/login", data={"username": "admin", "password": "Admin@123"})
H = {"Authorization": f"Bearer {r.json()['access_token']}"}

tasks = {}
for name, did in DOCS.items():
    rr = requests.post(f"{BASE}/kg/extract/{did}", headers=H, timeout=30)
    tasks[did] = rr.json().get("task_id")
    print(f"trigger {name} -> {rr.status_code} task={tasks[did]}", flush=True)

def counts(did):
    c = sqlite3.connect("doc_management.db", timeout=15)
    pref = f"doc:{did}#"
    m = c.execute("select count(1) from kg_entities where type='method' and canonical_id like ?", (pref+"%",)).fetchone()[0]
    s = c.execute("select count(1) from kg_entities where type in ('section','annex') and canonical_id like ?", (pref+"%",)).fetchone()[0]
    rel = c.execute("select status, message from background_tasks where id=?", (tasks[did],)).fetchone()
    c.close()
    return m, s, rel

t0 = time.perf_counter()
done = set()
while len(done) < len(DOCS):
    time.sleep(15)
    el = int(time.perf_counter() - t0)
    line = []
    for name, did in DOCS.items():
        m, s, rel = counts(did)
        st = rel[0] if rel else "?"
        line.append(f"{name}: {st} method={m} sec={s}")
        if st in ("completed", "failed"):
            done.add(did)
    print(f"[{el:>4}s] " + " | ".join(line), flush=True)

print("\n=== KG extraction done for both ===", flush=True)
