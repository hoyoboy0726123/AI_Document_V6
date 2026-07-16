"""Find pages in the FULL MIL-STD-810H whose layout detector reports a figure/chart/image."""
import os, sys, io
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("OCR_ENGINE", "rapid"); os.environ.setdefault("OCR_MODEL_TIER", "server")
os.environ.setdefault("OCR_VERSION", "PP-OCRv5"); os.environ.setdefault("OCR_DEVICE", "gpu")
import numpy as np
from PIL import Image
import fitz
from app.services import rapid_ocr
from app.services.pdf_image import pdf_page_to_image
from app.core.config import settings

FULL = r"C:\Users\G635LXG\Downloads\RAG\sample_pdfs\MIL-STD-810H.pdf"
doc = fitz.open(FULL)

# Candidate pages: those whose text contains a "FIGURE 5xx" caption (1-indexed).
cands = []
for i in range(len(doc)):
    t = doc[i].get_text("text")
    if "FIGURE" in t.upper():
        cands.append(i + 1)
print(f"pages mentioning FIGURE: {len(cands)} (first 15: {cands[:15]})", flush=True)

engines = rapid_ocr._get_engines()
hits = []
for pg in cands[:40]:
    b = pdf_page_to_image(FULL, pg, dpi=settings.OCR_DPI, max_dimension=settings.OCR_MAX_DIMENSION)
    arr = np.asarray(Image.open(io.BytesIO(b)).convert("RGB"))
    names = [str(x).lower() for x in (engines["layout"](arr).class_names or [])]
    figs = [x for x in names if x in {"figure", "chart", "image"}]
    if figs:
        print(f"  page {pg}: figure-ish regions = {figs}  | all = {sorted(set(names))}", flush=True)
        hits.append(pg)
    if len(hits) >= 5:
        break
print("FIGURE_PAGES=" + ",".join(map(str, hits)), flush=True)
