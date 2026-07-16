"""OCR speed benchmark: rapid engine, server tier, PP-OCRv5, GPU.
Times per-page inference on the 27-page MIL-810H sample.
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Force the GPU/server config regardless of .env, so the harness is explicit.
os.environ.setdefault("OCR_ENGINE", "rapid")
os.environ.setdefault("OCR_MODEL_TIER", "server")
os.environ.setdefault("OCR_VERSION", "PP-OCRv5")
os.environ.setdefault("OCR_DEVICE", "gpu")

SAMPLE = r"C:\Users\G635LXG\Downloads\RAG\sample_pdfs\MIL-810H_sample_27p.pdf"

import onnxruntime as ort
try:
    ort.preload_dlls()  # 1.22+: add nvidia-* wheel bin dirs to the DLL search path
    print("preload_dlls() ok", flush=True)
except Exception as e:
    print("preload_dlls n/a:", e, flush=True)
print("ORT providers:", ort.get_available_providers(), flush=True)

from app.services.pdf_image import get_pdf_page_count
from app.services import rapid_ocr

n = get_pdf_page_count(SAMPLE)
print(f"sample pages: {n}", flush=True)

# Warm-up: build engines + run page 1 (includes one-time model download + GPU init)
t0 = time.perf_counter()
engines = rapid_ocr._get_engines()
t_build = time.perf_counter() - t0
print(f"[setup] engine build (after models cached): {t_build:.1f}s", flush=True)

t0 = time.perf_counter()
b1 = rapid_ocr.rapid_page_blocks(engines, SAMPLE, 1)
t_first = time.perf_counter() - t0
print(f"[warmup] page 1 (incl. first-run model download if any): {t_first:.1f}s, blocks={len(b1)}", flush=True)

# Timed loop over all pages
times = []
total_blocks = 0
sample_text = ""
for pg in range(1, n + 1):
    t0 = time.perf_counter()
    blocks = rapid_ocr.rapid_page_blocks(engines, SAMPLE, pg)
    dt = time.perf_counter() - t0
    times.append(dt)
    total_blocks += len(blocks)
    if pg == 3:
        sample_text = "\n".join(b.text for b in blocks)[:600]
    print(f"  page {pg:>2}: {dt:6.2f}s  blocks={len(blocks)}", flush=True)

avg = sum(times) / len(times)
print("\n==== OCR (rapid/server/PP-OCRv5/GPU) ====", flush=True)
print(f"pages={n}  total={sum(times):.1f}s  avg/page={avg:.2f}s  total_blocks={total_blocks}", flush=True)
print("\n--- sample extracted text (page 3, first 600 chars) ---", flush=True)
print(sample_text, flush=True)
