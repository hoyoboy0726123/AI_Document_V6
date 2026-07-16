"""Batch-ingest given PDFs as plain text via the HTTP API. KG is off (KG_AUTO_EXTRACT=False)."""
import os, sys, time, uuid, shutil, requests

BASE = "http://127.0.0.1:8001/api/v1"
TMP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "storage", "tmp")
MILDIR = r"C:\Users\G635LXG\Downloads\RAG\sample_pdfs\mil"

# (title, filename in sample_pdfs/mil/)
DOCS = [
    ("MIL-HDBK-310 Global Climatic Data", "MIL-HDBK-310.pdf"),
    ("AR 70-38 Materiel for Extreme Climatic Conditions", "AR_70-38.pdf"),
]
if len(sys.argv) > 1:  # allow override: pairs title=file
    DOCS = [(t, f) for t, f in (a.split("=", 1) for a in sys.argv[1:])]

r = requests.post(f"{BASE}/auth/login", data={"username": "admin", "password": "Admin@123"})
r.raise_for_status()
H = {"Authorization": f"Bearer {r.json()['access_token']}"}
os.makedirs(TMP, exist_ok=True)
print("login ok\n", flush=True)

for title, fname in DOCS:
    src = os.path.join(MILDIR, fname)
    if not os.path.exists(src):
        print(f"SKIP {fname}: not found", flush=True); continue
    tmp = os.path.join(TMP, f"{uuid.uuid4()}.pdf")
    shutil.copyfile(src, tmp)
    payload = {"title": title, "source_pdf_path": tmp, "segments": None,
               "is_image_based": False, "force_vision": False, "force_ocr": False,
               "metadata": {}, "original_filename": fname}
    rr = requests.post(f"{BASE}/documents", json=payload, headers=H)
    if rr.status_code not in (200, 201):
        print(f"FAIL create {title}: {rr.status_code} {rr.text[:160]}", flush=True); continue
    doc_id = rr.json()["id"]
    t0 = time.perf_counter()
    while True:
        time.sleep(8)
        tr = requests.get(f"{BASE}/tasks/", headers=H, timeout=30)
        vec = next((t for t in tr.json() if t.get("document_id") == doc_id and t.get("task_type") == "vectorize"), None)
        el = int(time.perf_counter() - t0)
        if vec and vec["status"] in ("completed", "failed"):
            import sqlite3
            c = sqlite3.connect("doc_management.db"); n = c.execute(
                "select count(1) from document_chunks where document_id=?", (doc_id,)).fetchone()[0]; c.close()
            print(f"[{el:>3}s] {title}  -> {vec['status']}  chunks={n}  id={doc_id}", flush=True)
            break
        if el > 600:
            print(f"[{el}s] {title} timeout (still {vec['status'] if vec else '?'})", flush=True); break

print("\n=== batch ingest done ===", flush=True)
