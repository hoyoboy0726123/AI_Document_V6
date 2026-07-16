"""Re-trigger full KG extraction for the ingested doc and monitor to completion."""
import time, sqlite3, requests

BASE = "http://127.0.0.1:8001/api/v1"
DOC = "ccc3fd3f-6849-488c-a459-5ddc53f7b875"

r = requests.post(f"{BASE}/auth/login", data={"username": "admin", "password": "Admin@123"})
r.raise_for_status()
H = {"Authorization": f"Bearer {r.json()['access_token']}"}
print("login ok", flush=True)

r = requests.post(f"{BASE}/kg/extract/{DOC}", headers=H, timeout=30)
print(f"trigger kg/extract -> HTTP {r.status_code}: {str(r.text)[:200]}", flush=True)

def counts():
    c = sqlite3.connect("doc_management.db", timeout=15)
    e = c.execute("select count(1) from kg_entities").fetchone()[0]
    rel = c.execute("select count(1) from kg_relations").fetchone()[0]
    c.close()
    return e, rel

t0 = time.perf_counter()
last = None
while True:
    time.sleep(30)
    el = int(time.perf_counter() - t0)
    try:
        tr = requests.get(f"{BASE}/tasks/", headers=H, timeout=20)
        tasks = [t for t in tr.json() if t.get("document_id") == DOC and t.get("task_type") == "kg_extract"]
        task = max(tasks, key=lambda t: t.get("id", ""), default=None)
    except Exception as ex:
        task = None
        print(f"[{el}s] tasks poll err: {ex}", flush=True)
    e, rel = counts()
    if task:
        line = f"[{el:>5}s] kg_extract: {task['status']} {task.get('progress',0)}%  {task.get('message','')}  | entities={e} relations={rel}"
        if line != last:
            print(line, flush=True)
            last = line
        if task["status"] in ("completed", "failed"):
            print(f"\n=== kg_extract {task['status']} after {el}s | entities={e} relations={rel} ===", flush=True)
            break
