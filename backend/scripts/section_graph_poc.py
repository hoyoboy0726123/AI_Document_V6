"""Validate the section->spec graph end-to-end on a small page slice. ZERO DB touch.

Slices a page range out of MIL-STD-810H, runs kg_headings.build_section_spec_map,
and prints the Section tree (part_of) + Section->Standard(references) edges that the
KG pipeline would create. Proves 'Method X' becomes a node with its sub-tree and
the specs it references — without re-ingest or touching the production DB.

Usage: uv run python scripts/section_graph_poc.py [start] [end]   (1-indexed inclusive)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fitz
from app.services import kg_headings

PDF = r"C:\Users\G635LXG\Downloads\RAG\sample_pdfs\MIL-STD-810H.pdf"
start = int(sys.argv[1]) if len(sys.argv) > 1 else 136   # Method 504.3 (cites ASTM)
end = int(sys.argv[2]) if len(sys.argv) > 2 else 155

src = fitz.open(PDF)
out = fitz.open()
out.insert_pdf(src, from_page=start - 1, to_page=end - 1)
pdf_bytes = out.tobytes()

headings, section_specs, doc_specs = kg_headings.build_section_spec_map(pdf_bytes)

print(f"=== pages {start}-{end}: {len(headings)} headings ===\n")
print("Section tree (Document --contains--> / Section --part_of--> parent):")
for h in headings:
    indent = "  " * (h.number.count(".") + (1 if h.kind in ("method", "annex") else 0))
    par = f"  ⟂part_of→ {h.parent}" if h.parent else ("  (under method/doc)" if h.kind == "section" else "")
    specs = section_specs.get(h.number)
    ref = f"   ──references──> {sorted(specs)}" if specs else ""
    print(f"  {indent}[{h.kind:<7}] {h.number:<9} | {h.title[:48]}{par}{ref}")

print(f"\nSection→Standard reference edges: {sum(len(v) for v in section_specs.values())}")
for num, specs in section_specs.items():
    print(f"  {num}  ──references──>  {sorted(specs)}")
print(f"\nDocument-level specs (outside any section): {sorted(doc_specs)}")
