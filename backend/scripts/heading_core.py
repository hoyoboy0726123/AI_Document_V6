"""Core-layer heading detector (production candidate) — pure pdfminer, table-immune.

Keeps ONLY robust, generic heading shapes:
  - METHOD <n>           (e.g. METHOD 510.7)
  - ANNEX <A>
  - numbered section     N(.N){0,3} + real title   (e.g. 4.1.1.2 Relative Humidity)
  - ALL-CAPS standalone title (doc/method subtitle, >=3 letters)
Everything else (incl. bold table-cell labels, callouts, TOC bare numbers) is dropped.

Numbered headings are inherently table-immune: table cells like "Section 1: General",
"No.", "Response" never match "N.N  Title", so the core layer self-filters tables.

Usage: uv run python scripts/heading_core.py [start] [end]
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
_ANNEX_RE = re.compile(r"^\s*ANNEX\s+([A-Z])\b", re.IGNORECASE)
_NUM_RE = re.compile(r"^\s*(\d+(?:\.\d+){0,3})\.?\s+([A-Za-z].{2,70})$")
_CALLOUT = {"WARNING", "NOTE", "NOTES", "CAUTION", "DANGER", "CONTENTS", "PARAGRAPH",
            "PAGE", "FIGURE", "TABLE", "CONTENTS - CONTINUED"}


def line_metrics(line):
    sizes, bold = [], 0
    for ch in line:
        if isinstance(ch, LTChar):
            sizes.append(round(ch.size, 1))
            if "bold" in (ch.fontname or "").lower():
                bold += 1
    if not sizes:
        return None
    return Counter(sizes).most_common(1)[0][0], (bold > len(sizes) * 0.6)


raw_pages, all_sizes, bold_lines, total = [], [], 0, 0
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
                    if txt:
                        all_sizes.append(dom); total += 1
                        if bold:
                            bold_lines += 1
                        lines.append((ln.bbox, dom, bold, txt, page.height))
    raw_pages.append(lines)

body = statistics.median(all_sizes) if all_sizes else 10.0
bold_signal = (bold_lines / max(1, total)) < 0.5
prominent = lambda dom, bold: dom >= body + 1 or (bold and bold_signal)

kept = Counter()
heads = []
dropped_bold = []   # prominent lines the core layer dropped (should be table cells / noise)
for idx, lines in enumerate(raw_pages):
    pg = start + idx
    toc = any("CONTENTS" in t.upper() for (_, _, _, t, _) in lines)
    skip = set()
    for i, (bbox, dom, bold, txt, ph) in enumerate(lines):
        if i in skip or not prominent(dom, bold):
            continue
        x0, y0, x1, y1 = bbox
        if y0 > ph * 0.93 or y1 < ph * 0.07 or len(txt) > 90 or toc:
            continue
        mM, mA, mN = _METHOD_RE.match(txt), _ANNEX_RE.match(txt), _NUM_RE.match(txt)
        if mM:
            title = txt
            if i + 1 < len(lines):
                nxt = lines[i + 1][3].strip()
                if nxt and nxt.upper() == nxt and not _NUM_RE.match(nxt) and len(nxt) < 60:
                    title = f"{txt} — {nxt}"; skip.add(i + 1)
            heads.append(("method", mM.group(1), pg, title)); kept["method"] += 1
        elif mA:
            heads.append(("annex", mA.group(1), pg, txt)); kept["annex"] += 1
        elif mN:
            heads.append(("section", mN.group(1), pg, txt)); kept["section"] += 1
        elif txt.upper() == txt and sum(c.isalpha() for c in txt) >= 3 and txt.rstrip(".") not in _CALLOUT:
            heads.append(("title", "-", pg, txt)); kept["title"] += 1
        else:
            dropped_bold.append((pg, txt[:70]))

print(f"pages {start}-{end} | body size={body} bold-signal={bold_signal} | kept={dict(kept)} total={len(heads)}")
print(f"  dropped prominent (table-cells/noise) = {len(dropped_bold)}")
depths = [h[1].count('.') + 1 for h in heads if h[0] == 'section' and h[1][0].isdigit()]
print(f"  numbered-section max depth = {max(depths) if depths else 0}")
print("  headings:")
for kind, num, pg, title in heads:
    print(f"    [{kind:<7}] {num:<9} p{pg} | {title[:66]}")
if dropped_bold:
    print("  -- sample dropped (should be table/noise, NOT real sections) --")
    for pg, t in dropped_bold[:8]:
        print(f"    p{pg} | {t}")
