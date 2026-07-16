"""Batch-download the free public-domain MIL standards (EverySpec) into sample_pdfs/mil/."""
import os, time, requests

OUT = r"C:\Users\G635LXG\Downloads\RAG\sample_pdfs\mil"
os.makedirs(OUT, exist_ok=True)

SPECS = {
"MIL-HDBK-310": "https://everyspec.com/MIL-HDBK/MIL-HDBK-0300-0499/download.php?spec=MIL_HDBK_310.1851.pdf",
"MIL-HDBK-454": "https://everyspec.com/MIL-HDBK/MIL-HDBK-0300-0499/download.php?spec=MIL-HDBK-454.009165.pdf",
"MIL-HDBK-704-8": "https://everyspec.com/MIL-HDBK/MIL-HDBK-0700-0799/download.php?spec=MIL-HDBK-704-8.014620.PDF",
"MIL-HDBK-781A": "https://everyspec.com/MIL-HDBK/MIL-HDBK-0700-0799/download.php?spec=MIL_HDBK_781A.1933.pdf",
"MIL-STD-3033": "https://everyspec.com/MIL-STD/MIL-STD-3000-9999/download.php?spec=MIL-STD-3033.022904.PDF",
"MIL-STD-167-1A": "https://everyspec.com/MIL-STD/MIL-STD-0100-0299/download.php?spec=MIL-STD-167-1A.022418.PDF",
"MIL-STD-331D": "https://everyspec.com/MIL-STD/MIL-STD-0300-0499/download.php?spec=MIL-STD-331D.055733.pdf",
"MIL-STD-461G": "https://everyspec.com/MIL-STD/MIL-STD-0300-0499/download.php?spec=MIL-STD-461G.053915.pdf",
"MIL-STD-704F": "https://everyspec.com/MIL-STD/MIL-STD-0700-0799/download.php?spec=MIL-STD-704F.027323.pdf",
"MIL-STD-740-1": "https://everyspec.com/MIL-STD/MIL-STD-0700-0799/download.php?spec=MIL-STD-740-1.010379.PDF",
"MIL-STD-740-2": "https://everyspec.com/MIL-STD/MIL-STD-0700-0799/download.php?spec=MIL-STD-740-2.010380.PDF",
"MIL-STD-882E": "https://everyspec.com/MIL-STD/MIL-STD-0800-0899/download.php?spec=MIL-STD-882E.041682.pdf",
"MIL-STD-1275A": "https://everyspec.com/MIL-STD/MIL-STD-1100-1299/download.php?spec=MIL_STD_1275A.876.pdf",
"MIL-STD-1320D": "https://everyspec.com/MIL-STD/MIL-STD-1300-1399/download.php?spec=MIL-STD-1320D.050512.pdf",
"MIL-STD-1366E": "https://everyspec.com/MIL-STD/MIL-STD-1300-1399/download.php?spec=MIL-STD-1366E.002979.pdf",
"MIL-STD-1399B": "https://everyspec.com/MIL-STD/MIL-STD-1300-1399/download.php?spec=MIL_STD_1399B.731.pdf",
"MIL-STD-2105C": "https://everyspec.com/MIL-STD/MIL-STD-2000-2999/download.php?spec=MIL-STD-2105C.022387.PDF",
"MIL-STD-2218": "https://everyspec.com/MIL-STD/MIL-STD-2000-2999/download.php?spec=MIL-STD-2218.009149.PDF",
"MIL-STD-209K": "https://everyspec.com/MIL-STD/MIL-STD-0100-0299/download.php?spec=MIL-STD-209K.022319.PDF",
"MIL-DTL-12468D": "https://everyspec.com/MIL-SPECS/MIL-SPECS-MIL-DTL/download.php?spec=MIL-DTL-12468D.008409.PDF",
"MIL-DTL-83133J": "https://everyspec.com/MIL-SPECS/MIL-SPECS-MIL-DTL/download.php?spec=MIL-DTL-83133J.053582.pdf",
"MIL-DTL-901E": "https://everyspec.com/MIL-SPECS/MIL-SPECS-MIL-DTL/download.php?spec=MIL-DTL-901E.055988.pdf",
"MIL-PRF-14107D": "https://everyspec.com/MIL-PRF/MIL-PRF-010000-29999/download.php?spec=MIL-PRF-14107D.011700.pdf",
"MIL-PRF-2104L": "https://everyspec.com/MIL-PRF/MIL-PRF-000100-09999/download.php?spec=MIL-PRF-002104L.055744.pdf",
"MIL-PRF-23699F": "https://everyspec.com/MIL-PRF/MIL-PRF-010000-29999/download.php?spec=MIL-PRF-23699F.006702.pdf",
"MIL-PRF-32033A": "https://everyspec.com/MIL-PRF/MIL-PRF-030000-79999/download.php?spec=MIL-PRF-32033A_AMENDMENT-1.055120.pdf",
"MIL-PRF-372D": "https://everyspec.com/MIL-PRF/MIL-PRF-000100-09999/download.php?spec=MIL-PRF-372D.025058.pdf",
"MIL-PRF-46170E": "https://everyspec.com/MIL-PRF/MIL-PRF-030000-79999/download.php?spec=MIL-PRF-46170E.055957.pdf",
"MIL-PRF-5606H": "https://everyspec.com/MIL-PRF/MIL-PRF-000100-09999/download.php?spec=MIL-PRF-5606H.005993.PDF",
"MIL-PRF-6083F": "https://everyspec.com/MIL-PRF/MIL-PRF-000100-09999/download.php?spec=MIL-PRF-6083F.029916.pdf",
"MIL-PRF-63460E": "https://everyspec.com/MIL-PRF/MIL-PRF-030000-79999/download.php?spec=MIL-PRF-63460E.013239.PDF",
"MIL-PRF-680C": "https://everyspec.com/MIL-PRF/MIL-PRF-000100-09999/download.php?spec=MIL-PRF-680C_NOTICE-1.054285.pdf",
"MIL-PRF-7808L": "https://everyspec.com/MIL-PRF/MIL-PRF-000100-09999/download.php?spec=MIL-PRF-7808L.036173.pdf",
"MIL-PRF-83282D": "https://everyspec.com/MIL-PRF/MIL-PRF-080000-99999/download.php?spec=MIL-PRF-83282D.031137.pdf",
"MIL-PRF-85570D": "https://everyspec.com/MIL-PRF/MIL-PRF-080000-99999/download.php?spec=MIL-PRF-85570D.014144.PDF",
"MIL-PRF-85704C": "https://everyspec.com/MIL-PRF/MIL-PRF-080000-99999/download.php?spec=MIL-PRF-85704C.016833.PDF",
"MIL-PRF-87257C": "https://everyspec.com/MIL-PRF/MIL-PRF-080000-99999/download.php?spec=MIL-PRF-87257C.055953.pdf",
"MIL-PRF-87937D": "https://everyspec.com/MIL-PRF/MIL-PRF-080000-99999/download.php?spec=MIL-PRF-87937D.014734.PDF",
}

ok, fail = 0, 0
for name, url in SPECS.items():
    dest = os.path.join(OUT, name + ".pdf")
    if os.path.exists(dest) and os.path.getsize(dest) > 5000:
        print(f"skip (exists) {name}", flush=True); ok += 1; continue
    try:
        r = requests.get(url, timeout=120, headers={"User-Agent": "Mozilla/5.0"})
        head = r.content[:5]
        if r.status_code == 200 and head[:4] == b"%PDF":
            with open(dest, "wb") as f:
                f.write(r.content)
            print(f"OK   {name:<16} {len(r.content)//1024} KB", flush=True); ok += 1
        else:
            print(f"FAIL {name:<16} HTTP {r.status_code} head={head!r}", flush=True); fail += 1
    except Exception as e:
        print(f"FAIL {name:<16} {str(e)[:80]}", flush=True); fail += 1
    time.sleep(1)

print(f"\n=== done: {ok} ok, {fail} fail → {OUT} ===", flush=True)
