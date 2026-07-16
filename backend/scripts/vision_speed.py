"""Vision (LLM) speed benchmark: force_vision path with gemma4:12b via Ollama.
Mirrors documents.py: render page (dpi=80, max_dim=768) -> ai.extract_text_with_vision.
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SAMPLE = r"C:\Users\G635LXG\Downloads\RAG\sample_pdfs\MIL-810H_sample_27p.pdf"

from app.services.pdf_image import get_pdf_page_count, pdf_pages_to_images
from app.services import ai
from app.core.config import settings

print("vision model:", settings.OLLAMA_VISION_MODEL, flush=True)
n = get_pdf_page_count(SAMPLE)
print(f"sample pages: {n}", flush=True)

times, lens = [], []
sample_text = ""
for pg in range(1, n + 1):
    imgs = pdf_pages_to_images(SAMPLE, [pg], dpi=80, max_dimension=768)
    t0 = time.perf_counter()
    segs = ai.extract_text_with_vision(imgs, [pg])
    dt = time.perf_counter() - t0
    tlen = sum(len(s.get("text", "")) for s in segs)
    times.append(dt); lens.append(tlen)
    if pg == 3:
        sample_text = "\n".join(s.get("text", "") for s in segs)[:600]
    tag = " (incl. model load)" if pg == 1 else ""
    print(f"  page {pg:>2}: {dt:6.2f}s  chars={tlen}{tag}", flush=True)

warm = times[1:] if len(times) > 1 else times
avg_all = sum(times) / len(times)
avg_warm = sum(warm) / len(warm)
print(f"\n==== VISION (force_vision / {settings.OLLAMA_VISION_MODEL} / Ollama GPU) ====", flush=True)
print(f"pages={n}  total={sum(times):.1f}s  avg/page(all)={avg_all:.2f}s  avg/page(excl. p1 load)={avg_warm:.2f}s", flush=True)
print("\n--- sample VL markdown (page 3, first 600 chars) ---", flush=True)
print(sample_text, flush=True)
