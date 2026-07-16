"""Delete the empty/low MIL docs and re-ingest them (text, or OCR for scanned)."""
import os, time, uuid, shutil, sqlite3, requests

BASE = "http://127.0.0.1:8001/api/v1"
TMP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "storage", "tmp")
MILDIR = r"C:\Users\G635LXG\Downloads\RAG\sample_pdfs\mil"

def login():
    r = requests.post(f"{BASE}/auth/login", data={"username": "admin", "password": "Admin@123"})
    r.raise_for_status(); return {"Authorization": f"Bearer {r.json()['access_token']}"}

H = login()

# title -> (filename, force_ocr)
FIX = {
    "MIL-HDBK-781A":  ("MIL-HDBK-781A.pdf", False),
    "MIL-PRF-46170E": ("MIL-PRF-46170E.pdf", False),
    "MIL-PRF-6083F":  ("MIL-PRF-6083F.pdf", False),
    "MIL-STD-740-1":  ("MIL-STD-740-1.pdf", True),   # scanned -> OCR
    "MIL-STD-740-2":  ("MIL-STD-740-2.pdf", True),   # scanned -> OCR
}

# 1) delete existing (empty) doc rows by title
c = sqlite3.connect("doc_management.db")
ids = {t: c.execute("select id from documents where title=?", (t,)).fetchone() for t in FIX}
c.close()
for t, row in ids.items():
    if row:
        rr = requests.delete(f"{BASE}/documents/{row[0]}", headers=H)
        print(f"delete {t}: {rr.status_code}", flush=True)

os.makedirs(TMP, exist_ok=True)
def chunks(did):
    c = sqlite3.connect("doc_management.db"); n = c.execute(
        "select count(1) from document_chunks where document_id=?", (did,)).fetchone()[0]; c.close(); return n

# 2) re-ingest
for title, (fname, ocr) in FIX.items():
    src = os.path.join(MILDIR, fname)
    tmp = os.path.join(TMP, f"{uuid.uuid4()}.pdf"); shutil.copyfile(src, tmp)
    payload = {"title": title, "source_pdf_path": tmp, "segments": None,
               "is_image_based": ocr, "force_vision": False, "force_ocr": ocr,
               "metadata": {"source": "everyspec-mil"}, "original_filename": fname}
    rr = requests.post(f"{BASE}/documents", json=payload, headers=H, timeout=900)
    if rr.status_code not in (200, 201):
        print(f"FAIL create {title}: {rr.status_code} {rr.text[:120]}", flush=True); continue
    did = rr.json()["id"]; t0 = time.perf_counter()
    while True:
        time.sleep(10)
        try:
            tr = requests.get(f"{BASE}/tasks/", headers=H, timeout=30)
            if tr.status_code == 401: H = login(); continue
            task = next((x for x in tr.json() if x.get("document_id") == did and x.get("task_type") in ("vectorize","ocr")), None)
        except Exception: task = None
        el = int(time.perf_counter() - t0)
        if task and task["status"] in ("completed", "failed"):
            print(f"{title:<16} {task['task_type']}->{task['status']} chunks={chunks(did)} ({el}s){' [OCR]' if ocr else ''}", flush=True); break
        if el > 1200:
            print(f"{title}: timeout chunks={chunks(did)}", flush=True); break

print("\n=== fix done ===", flush=True)
