import os, sys, io
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("OCR_ENGINE", "rapid"); os.environ.setdefault("OCR_MODEL_TIER", "server")
os.environ.setdefault("OCR_VERSION", "PP-OCRv5"); os.environ.setdefault("OCR_DEVICE", "gpu")
import numpy as np
from PIL import Image
from app.services import rapid_ocr, ai
from app.services.pdf_image import pdf_page_to_image, pdf_pages_to_images
from app.core.config import settings

FULL = r"C:\Users\G635LXG\Downloads\RAG\sample_pdfs\MIL-STD-810H.pdf"
PG = int(sys.argv[1]) if len(sys.argv) > 1 else 10
OUTPNG = rf"C:\Users\G635LXG\Downloads\RAG\sample_pdfs\page{PG}.png"

# Render page for the user to see the actual figure.
b = pdf_page_to_image(FULL, PG, dpi=140, max_dimension=1600)
Image.open(io.BytesIO(b)).save(OUTPNG)
print("PAGE_PNG=" + OUTPNG, flush=True)

engines = rapid_ocr._get_engines()
arr = np.asarray(Image.open(io.BytesIO(pdf_page_to_image(FULL, PG, dpi=settings.OCR_DPI, max_dimension=settings.OCR_MAX_DIMENSION))).convert("RGB"))
names = [str(x).lower() for x in (engines["layout"](arr).class_names or [])]
print(f"\nlayout regions on page {PG}: {sorted(set(names))}", flush=True)
print(f"  -> image/figure/chart regions present: {[x for x in names if x in {'image','figure','chart'}]}  (these are in _SKIP_LABELS)", flush=True)

# OCR path
ocr_blocks = rapid_ocr.rapid_page_blocks(engines, FULL, PG)
ocr_text = "\n".join(bk.text for bk in ocr_blocks)
print(f"\n[OCR] kept {len(ocr_blocks)} blocks (text+table only). The image region is DROPPED.", flush=True)
print(f"[OCR] does output describe the figure? -> {'[IMAGE' in ocr_text}", flush=True)
print("[OCR] last 220 chars:\n   " + ocr_text[-220:].replace(chr(10), ' '), flush=True)

# VL path
segs = ai.extract_text_with_vision(pdf_pages_to_images(FULL, [PG], dpi=80, max_dimension=768), [PG])
vl = "\n".join(s.get("text", "") for s in segs)
imglines = [l for l in vl.splitlines() if "[IMAGE" in l.upper()]
print(f"\n[VL/{settings.OLLAMA_VISION_MODEL}] chars={len(vl)}, [IMAGE:...] description lines={len(imglines)}", flush=True)
for l in imglines:
    print("   " + l.strip()[:400], flush=True)
