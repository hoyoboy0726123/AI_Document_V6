"""PoC v2: adaptive, generic heading detection with noise handling.

Signal is ADAPTIVE per document, so it generalises:
  - measure body baseline: median font size + fraction-of-text-that-is-bold
  - a line is "prominent" if  size >= body_median + 1   OR   (bold AND body is mostly non-bold)
Then keep only heading-SHAPED lines and drop noise:
  - require short line, not in top/bottom margin (header/footer)
  - numbered heading must have a real title after the number (kills TOC bare numbers)
  - drop callout words (WARNING/NOTE/CAUTION/FIGURE/TABLE...) that carry no section number
  - merge a METHOD line with its following ALL-CAPS subtitle
Classify: method | section(number,parent) | title(unnumbered-but-prominent).

Usage: uv run python scripts/heading_poc2.py [start] [end]    (1-indexed inclusive)
"""
import sys, re, statistics
from collections import Counter
from pdfminer.high_level import extract_pages
from pdfminer.layout import LTTextContainer, LTTextLine, LTChar

PDF = r"C:\Users\G635LXG\Downloads\RAG\sample_pdfs\MIL-STD-810H.pdf"
start = int(sys.argv[1]) if len(sys.argv) > 1 else 263
end = int(sys.argv[2]) if len(sys.argv) > 2 else 272
page_numbers = list(range(start - 1, end))

_METHOD_RE = re.compile(r"^\s*METHOD\s+(\d+(?:\.\d+)?)\b", re.IGNORECASE)
_NUM_RE = re.compile(r"^\s*(\d+(?:\.\d+){0,3})\.?\s+(\S.*\S|\S)\s*$")   # number + real title
_ANNEX_RE = re.compile(r"^\s*ANNEX\s+([A-Z])\b", re.IGNORECASE)
_CALLOUT = {"WARNING", "NOTE", "NOTES", "CAUTION", "DANGER", "CONTENTS", "PARAGRAPH", "PAGE",
            "FIGURE", "TABLE", "CONTENTS - CONTINUED"}


def line_metrics(line):
    sizes, bold = [], 0
    for ch in line:
        if isinstance(ch, LTChar):
            sizes.append(round(ch.size, 1))
            if "bold" in (ch.fontname or "").lower():
                bold += 1
    if not sizes:
        return None
    dom = Counter(sizes).most_common(1)[0][0]
    return dom, (bold > len(sizes) * 0.6)


# ---- pass 1: collect lines + baseline ----
raw_pages = []
all_sizes, bold_lines, total_lines = [], 0, 0
for page in extract_pages(PDF, page_numbers=page_numbers):
    lines = []
    for el in page:
        if isinstance(el, LTTextContainer):
            for ln in el:
                if isinstance(ln, LTTextLine):
                    m = line_metrics(ln)
                    if not m:
                        continue
                    dom, bold = m
                    txt = ln.get_text().strip()
                    if not txt:
                        continue
                    all_sizes.append(dom); total_lines += 1
                    if bold:
                        bold_lines += 1
                    lines.append((ln.bbox, dom, bold, txt, page.height))
    raw_pages.append(lines)

body_size = statistics.median(all_sizes) if all_sizes else 10.0
bold_frac = bold_lines / max(1, total_lines)
bold_is_signal = bold_frac < 0.5     # if body text isn't mostly bold, bold marks headings
print(f"pages {start}-{end} | body median size={body_size} | bold fraction={bold_frac:.2f} "
      f"| heading signal = {'BOLD' if bold_is_signal else ''}{' + ' if bold_is_signal else ''}size>{body_size}\n")


def prominent(dom, bold):
    return dom >= body_size + 1 or (bold and bold_is_signal)


# ---- pass 2: detect + classify + denoise ----
filtered = Counter()
headings = []
_OTHER_SAMPLES = []
for idx, lines in enumerate(raw_pages):
    pg = start + idx
    # skip a table-of-contents page: prominent lines that are mostly bare numbers / CONTENTS
    page_is_toc = any("CONTENTS" in t.upper() for (_, _, _, t, _) in lines)
    skip_idx = set()   # subtitle lines already merged into a METHOD heading
    for i, (bbox, dom, bold, txt, ph) in enumerate(lines):
        if i in skip_idx:
            continue
        if not prominent(dom, bold):
            continue
        x0, y0, x1, y1 = bbox
        if y0 > ph * 0.93 or y1 < ph * 0.07:
            filtered["header/footer"] += 1; continue
        if len(txt) > 90:
            filtered["too-long"] += 1; continue
        up = txt.upper().rstrip(".")
        if page_is_toc:
            filtered["toc-page"] += 1; continue
        m_method = _METHOD_RE.match(txt)
        m_annex = _ANNEX_RE.match(txt)
        m_num = _NUM_RE.match(txt)
        if m_method:
            title = txt
            if i + 1 < len(lines):       # merge following ALL-CAPS subtitle (and don't re-emit it)
                nxt = lines[i + 1][3].strip()
                if nxt and nxt.upper() == nxt and not _NUM_RE.match(nxt) and len(nxt) < 60:
                    title = f"{txt} — {nxt}"
                    skip_idx.add(i + 1)
            headings.append(("method", m_method.group(1), "(doc)", title))
        elif m_annex:
            headings.append(("annex", m_annex.group(1), "(doc)", txt))
        elif m_num:
            num = m_num.group(1)
            parent = num.rsplit(".", 1)[0] if "." in num else "(method/doc)"
            headings.append(("section", num, parent, txt))
        elif up in _CALLOUT:
            filtered["callout"] += 1
        elif txt.upper() == txt and sum(c.isalpha() for c in txt) >= 3:
            headings.append(("title", "-", "-", txt))   # unnumbered prominent title (needs real letters)
        else:
            filtered["other"] += 1
            _OTHER_SAMPLES.append((pg, dom, int(bold), txt[:80]))

print("=== detected headings (denoised) ===")
last_pg = None
for kind, num, parent, title in headings:
    print(f"  [{kind:<7}] {num:<10} parent={parent:<12} | {title[:72]}")
print(f"\n=== filtered noise: {dict(filtered)} ===")
print(f"total headings kept = {len(headings)}")
if _OTHER_SAMPLES:
    print("\n--- 'other' samples (prominent lines we dropped) ---")
    for pg, dom, bold, txt in _OTHER_SAMPLES[:25]:
        print(f"  p{pg} size={dom} bold={bold} | {txt}")
