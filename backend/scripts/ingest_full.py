"""Ingest the full MIL-STD-810H as PLAIN TEXT (pdfminer) via the real HTTP API, then watch progress."""
import os, sys, time, uuid, shutil, requests

BASE = "http://127.0.0.1:8001/api/v1"
SRC = r"C:\Users\G635LXG\Downloads\RAG\sample_pdfs\MIL-STD-810H.pdf"
TMP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "storage", "tmp")

# 1) login
r = requests.post(f"{BASE}/auth/login", data={"username": "admin", "password": "Admin@123"})
r.raise_for_status()
tok = r.json()["access_token"]
H = {"Authorization": f"Bearer {tok}"}
print("login ok", flush=True)

# 2) copy PDF into the backend temp dir
os.makedirs(TMP_DIR, exist_ok=True)
tmp_path = os.path.join(TMP_DIR, f"{uuid.uuid4()}.pdf")
shutil.copyfile(SRC, tmp_path)
print(f"copied -> {tmp_path}", flush=True)

# 3) create document in PLAIN TEXT mode (no force flags, no presupplied segments)
payload = {
    "title": "MIL-STD-810H (full, plain-text)",
    "source_pdf_path": tmp_path,
    "segments": None,
    "is_image_based": False,
    "force_vision": False,
    "force_ocr": False,
    "metadata": {},
    "original_filename": "MIL-STD-810H.pdf",
}
r = requests.post(f"{BASE}/documents", json=payload, headers=H)
r.raise_for_status()
doc = r.json()
doc_id = doc["id"]
print(f"document created id={doc_id}  (vectorize task running in background)", flush=True)

# 4) poll the vectorize task for this document
t0 = time.perf_counter()
last = None
while True:
    time.sleep(12)
    tr = requests.get(f"{BASE}/tasks/", headers=H)
    tr.raise_for_status()
    tasks = [t for t in tr.json() if t.get("document_id") == doc_id and t.get("task_type") in ("vectorize", "kg_extract")]
    vec = next((t for t in tasks if t["task_type"] == "vectorize"), None)
    kg = next((t for t in tasks if t["task_type"] == "kg_extract"), None)
    el = int(time.perf_counter() - t0)
    if vec:
        line = f"[{el:>4}s] vectorize: {vec['status']} {vec.get('progress',0)}%  {vec.get('message','')}"
        if kg:
            line += f"  || kg_extract: {kg['status']} {kg.get('progress',0)}% {kg.get('message','')}"
        if line != last:
            print(line, flush=True)
            last = line
        if vec["status"] in ("completed", "failed"):
            print(f"\n=== vectorize {vec['status']} after {el}s ===", flush=True)
            if vec["status"] == "completed":
                print("RAG is now queryable. (KG may still be running in background.)", flush=True)
            print(f"DOC_ID={doc_id}", flush=True)
            break
