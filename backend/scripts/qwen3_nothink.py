"""Test ways to disable thinking on qwen3-vl:8b: /no_think token vs think param."""
import os, sys, time, base64, requests
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.services.pdf_image import pdf_pages_to_images

SAMPLE = r"C:\Users\G635LXG\Downloads\RAG\sample_pdfs\MIL-810H_sample_27p.pdf"
BASE = ("Convert this PDF page into clean GitHub-Flavored Markdown. Output only Markdown. "
        "Use ## / ### headings, pipe-tables for tables, describe figures as [IMAGE: ...], "
        "include all visible text, no commentary.")

def run(pg, prompt, think=None, label=""):
    img = pdf_pages_to_images(SAMPLE, [pg], dpi=80, max_dimension=768)[0]
    b64 = base64.b64encode(img).decode()
    body = {"model": "qwen3-vl:8b",
            "messages": [{"role": "user", "content": prompt, "images": [b64]}],
            "stream": False}
    if think is not None:
        body["think"] = think
    t0 = time.perf_counter()
    r = requests.post("http://127.0.0.1:11434/api/chat", json=body, timeout=600)
    dt = time.perf_counter() - t0
    m = r.json().get("message", {})
    c = m.get("content", ""); th = m.get("thinking", "") or ""
    print(f"[{label}] page {pg}: {dt:6.2f}s  content={len(c)}  thinking={len(th)}", flush=True)
    return c

# A: /no_think token appended to prompt
run(3, BASE + "\n/no_think", label="/no_think tok")
run(4, BASE + "\n/no_think", label="/no_think tok")
# B: think param false AND /no_think token
run(3, BASE + "\n/no_think", think=False, label="no_think+param")
print("\n--- sample (page 3, /no_think, first 500 chars) ---", flush=True)
print(run(3, BASE + "\n/no_think", label="sample")[:500], flush=True)
