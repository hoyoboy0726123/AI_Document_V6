"""Fire several /kg/extract requests at once to prove they SERIALISE (no 'database is
locked' 500s) and complete one-by-one through the single worker."""
import time, sqlite3, requests

BASE = "http://127.0.0.1:8001/api/v1"
r = requests.post(f"{BASE}/auth/login", data={"username": "admin", "password": "Admin@123"})
H = {"Authorization": f"Bearer {r.json()['access_token']}"}

# pick 5 already-ingested MIL docs to re-extract (re-running clears+rebuilds their relations)
c = sqlite3.connect("doc_management.db")
docs = c.execute("select id,title from documents where title like 'MIL-PRF-%' limit 5").fetchall()
c.close()

print("firing 5 /kg/extract back-to-back...\n", flush=True)
tasks = []
for did, title in docs:
    rr = requests.post(f"{BASE}/kg/extract/{did}", headers=H, timeout=30)
    body = rr.json() if rr.status_code in (200, 202) else rr.text[:120]
    print(f"  {title:<16} HTTP {rr.status_code}  {body}", flush=True)
    if rr.status_code in (200, 202):
        tasks.append((rr.json()["task_id"], title))

print("\nwatching tasks complete (should finish one at a time)...", flush=True)
t0 = time.perf_counter()
done = set()
while len(done) < len(tasks):
    time.sleep(8)
    c = sqlite3.connect("doc_management.db", timeout=20)
    running = []
    for tid, title in tasks:
        st = c.execute("select status from background_tasks where id=?", (tid,)).fetchone()
        st = st[0] if st else "?"
        if st in ("completed", "failed") and tid not in done:
            done.add(tid); print(f"  [{int(time.perf_counter()-t0)}s] {title} -> {st}", flush=True)
        elif st == "running":
            running.append(title)
    c.close()
    if running and len(running) > 1:
        print(f"  !! {len(running)} running CONCURRENTLY: {running} (serialisation FAILED)", flush=True)

print(f"\n=== all {len(tasks)} done, max concurrent running should have been 1 ===", flush=True)
