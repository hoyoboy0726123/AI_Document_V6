"""PoC: layout/font-based heading detection (approach B) on a few MIL-STD-810H pages.

Reads font size + boldness per text line via pdfminer LTChar (BEFORE text is flattened),
flags lines that are visually prominent (bigger/bold) and short → heading candidates,
then parses a generic section/method number. Goal: confirm it catches 'METHOD 510.7'
and sub-section numbers that the current regex-on-flattened-text misses.

Usage: uv run python scripts/heading_poc.py [start_page] [end_page]   (1-indexed, inclusive)
"""
import sys, re, statistics
from collections import Counter
from pdfminer.high_level import extract_pages
from pdfminer.layout import LTTextContainer, LTTextLine, LTChar

PDF = r"C:\Users\G635LXG\Downloads\RAG\sample_pdfs\MIL-STD-810H.pdf"
start = int(sys.argv[1]) if len(sys.argv) > 1 else 263
end = int(sys.argv[2]) if len(sys.argv) > 2 else 272
page_numbers = list(range(start - 1, end))  # extract_pages is 0-indexed

# Generic heading-number parser: METHOD 5xx.x | dotted-decimal | lettered/roman-ish leading token.
_NUM_RE = re.compile(r"^\s*(METHOD\s+\d+(?:\.\d+)?|\d+(?:\.\d+){0,3}|[A-Z]\.|[IVX]+\.)\b", re.IGNORECASE)


def line_metrics(line):
    sizes, bold = [], 0
    for ch in line:
        if isinstance(ch, LTChar):
            sizes.append(round(ch.size, 1))
            if "bold" in (ch.fontname or "").lower():
                bold += 1
    if not sizes:
        return None
    dom = Counter(sizes).most_common(1)[0][0]      # dominant font size on the line
    return dom, max(sizes), (bold > len(sizes) * 0.5)


# Pass 1: collect all line font sizes across the range → body-text baseline (median of dominant sizes)
all_dom = []
pages_cache = []
for page in extract_pages(PDF, page_numbers=page_numbers):
    lines = []
    for el in page:
        if isinstance(el, LTTextContainer):
            for ln in el:
                if isinstance(ln, LTTextLine):
                    m = line_metrics(ln)
                    if not m:
                        continue
                    dom, mx, bold = m
                    txt = ln.get_text().strip()
                    if txt:
                        all_dom.append(dom)
                        lines.append((ln.bbox, dom, mx, bold, txt, page.height))
    pages_cache.append(lines)

body = statistics.median(all_dom) if all_dom else 10.0
print(f"pages {start}-{end} | lines={len(all_dom)} | body-text median font size = {body}\n")
print("=== heading candidates (bigger-than-body OR bold, short line) ===\n")

for idx, lines in enumerate(pages_cache):
    pg = start + idx
    hits = []
    for (bbox, dom, mx, bold, txt, ph) in lines:
        x0, y0, x1, y1 = bbox
        is_big = dom >= body + 0.5
        short = len(txt) <= 80
        margin = (y0 > ph * 0.93) or (y1 < ph * 0.07)   # near top/bottom → likely header/footer
        if (is_big or bold) and short and txt:
            num = _NUM_RE.match(txt)
            tag = "HEADER/FOOTER?" if margin else ("§" + num.group(1) if num else "(no-num)")
            hits.append((dom, bold, tag, txt[:70]))
    if hits:
        print(f"--- page {pg} ---")
        for dom, bold, tag, txt in hits:
            print(f"  size={dom:<5} bold={int(bold)} {tag:<16} | {txt}")
        print()
