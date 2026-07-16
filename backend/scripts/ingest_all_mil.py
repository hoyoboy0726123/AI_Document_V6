"""Ingest ALL PDFs in sample_pdfs/mil/ as plain text (KG auto-extracts after each).
Skips files whose title already exists in the DB. Title = filename stem (helps KG link
the document to its spec node)."""
import os, glob, time, uuid, shutil, sqlite3, requests

BASE = "http://127.0.0.1:8001/api/v1"
TMP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "storage", "tmp")
MILDIR = r"C:\Users\G635LXG\Downloads\RAG\sample_pdfs\mil"

def login():
    for attempt in range(6):
        r = requests.post(f"{BASE}/auth/login", data={"username": "admin", "password": "Admin@123"})
        if r.status_code == 429:  # rate-limited → back off
            time.sleep(20 + attempt * 10); continue
        r.raise_for_status()
        return {"Authorization": f"Bearer {r.json()['access_token']}"}
    raise RuntimeError("login rate-limited repeatedly")

H = login()
os.makedirs(TMP, exist_ok=True)

def existing_titles():
    c = sqlite3.connect("doc_management.db"); rows = c.execute("select title from documents").fetchall(); c.close()
    return {t[0] for t in rows}

def chunk_count(did):
    c = sqlite3.connect("doc_management.db"); n = c.execute(
        "select count(1) from document_chunks where document_id=?", (did,)).fetchone()[0]; c.close()
    return n

have = existing_titles()
files = sorted(glob.glob(os.path.join(MILDIR, "*.pdf")))
# the climatic-data handbook + the army reg already ingested earlier under longer titles:
SKIP_FILES = {"MIL-HDBK-310.pdf", "AR_70-38.pdf"}
print(f"login ok | {len(files)} pdfs in folder | {len(have)} docs already in DB\n", flush=True)

ok = skip = fail = 0
for path in files:
    fname = os.path.basename(path)
    title = os.path.splitext(fname)[0]
    if fname in SKIP_FILES or title in have:
        print(f"SKIP  {title} (already ingested)", flush=True); skip += 1; continue
    tmp = os.path.join(TMP, f"{uuid.uuid4()}.pdf")
    shutil.copyfile(path, tmp)
    payload = {"title": title, "source_pdf_path": tmp, "segments": None,
               "is_image_based": False, "force_vision": False, "force_ocr": False,
               "metadata": {"source": "everyspec-mil"}, "original_filename": fname}
    rr = None
    for attempt in range(3):
        try:
            rr = requests.post(f"{BASE}/documents", json=payload, headers=H, timeout=600)
        except Exception as e:
            print(f"WARN  {title}: create exception {str(e)[:80]} (attempt {attempt})", flush=True)
            time.sleep(10); continue
        if rr.status_code in (200, 201):
            break
        if rr.status_code == 401:          # token expired → re-login (only then)
            time.sleep(2); H = login(); continue
        time.sleep(5)                      # other transient → retry
    if not rr or rr.status_code not in (200, 201):
        print(f"FAIL  {title}: create {rr.status_code if rr else '?'} {(rr.text[:120] if rr else '')}", flush=True); fail += 1; continue
    did = rr.json()["id"]
    t0 = time.perf_counter()
    while True:
        time.sleep(8)
        try:
            tr = requests.get(f"{BASE}/tasks/", headers=H, timeout=30)
            if tr.status_code == 401:
                H = login(); continue
            vec = next((t for t in tr.json() if t.get("document_id") == did and t.get("task_type") == "vectorize"), None)
        except Exception:
            vec = None
        el = int(time.perf_counter() - t0)
        if vec and vec["status"] in ("completed", "failed"):
            n = chunk_count(did)
            print(f"OK    {title:<18} {vec['status']:<9} chunks={n:<5} ({el}s)  id={did[:8]}", flush=True)
            ok += 1; break
        if el > 900:
            print(f"WARN  {title}: vectorize timeout (still {vec['status'] if vec else '?'})", flush=True)
            ok += 1; break

print(f"\n=== ingest done: {ok} ingested, {skip} skipped, {fail} failed ===", flush=True)
print("KG auto-extract tasks are now queued/running in the background.", flush=True)
