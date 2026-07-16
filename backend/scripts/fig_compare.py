"""Prove OCR drops figures while VL describes them. Find a figure page, run both."""
import os, sys, io, base64, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("OCR_ENGINE", "rapid")
os.environ.setdefault("OCR_MODEL_TIER", "server")
os.environ.setdefault("OCR_VERSION", "PP-OCRv5")
os.environ.setdefault("OCR_DEVICE", "gpu")

import numpy as np
from PIL import Image
from app.services.pdf_image import get_pdf_page_count, pdf_page_to_image, pdf_pages_to_images
from app.services import rapid_ocr, ai
from app.services.rapid_ocr import _SKIP_LABELS, _TABLE_LABELS
from app.core.config import settings

SAMPLE = r"C:\Users\G635LXG\Downloads\RAG\sample_pdfs\MIL-810H_sample_27p.pdf"
n = get_pdf_page_count(SAMPLE)
engines = rapid_ocr._get_engines()

# 1) Find the first page whose layout detector reports a figure/chart/image region.
target = None
for pg in range(1, n + 1):
    b = pdf_page_to_image(SAMPLE, pg, dpi=settings.OCR_DPI, max_dimension=settings.OCR_MAX_DIMENSION)
    arr = np.asarray(Image.open(io.BytesIO(b)).convert("RGB"))
    lout = engines["layout"](arr)
    names = [str(x).lower() for x in (lout.class_names or [])]
    figs = [x for x in names if x in {"figure", "chart", "image"}]
    if figs:
        print(f"page {pg}: layout labels = {sorted(set(names))}  -> figure-ish: {figs}", flush=True)
        target = pg
        break

if target is None:
    print("No figure/chart/image region detected in sample; nothing to compare.")
    sys.exit(0)

print(f"\n===== COMPARING PAGE {target} =====\n", flush=True)

# 2) OCR path
ocr_blocks = rapid_ocr.rapid_page_blocks(engines, SAMPLE, target)
kinds = {}
for b in ocr_blocks:
    kinds[b.block_type] = kinds.get(b.block_type, 0) + 1
print(f"[OCR] kept {len(ocr_blocks)} blocks, types={kinds}  (figure/chart/image regions are in _SKIP_LABELS -> DROPPED)", flush=True)
ocr_text = "\n".join(b.text for b in ocr_blocks)
print(f"[OCR] any '[IMAGE' / figure description? -> {'[IMAGE' in ocr_text or 'figure' in ocr_text.lower()}", flush=True)

# 3) VL path
imgs = pdf_pages_to_images(SAMPLE, [target], dpi=80, max_dimension=768)
segs = ai.extract_text_with_vision(imgs, [target])
vl_text = "\n".join(s.get("text", "") for s in segs)
img_blocks = [line for line in vl_text.splitlines() if "[IMAGE" in line.upper()]
print(f"\n[VL/{settings.OLLAMA_VISION_MODEL}] chars={len(vl_text)}, [IMAGE:...] description lines = {len(img_blocks)}", flush=True)
for line in img_blocks[:5]:
    print("   ", line.strip()[:300], flush=True)
