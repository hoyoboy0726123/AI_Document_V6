"""Fire a batch of RAG questions at the ingested MIL-STD-810H and print answers + sources."""
import requests, textwrap, time

BASE = "http://127.0.0.1:8001/api/v1"

def post_retry(path, *, attempts=8, **kw):
    """Retry on 500 / SQLite-lock contention (KG extraction hammers the DB)."""
    for a in range(1, attempts + 1):
        try:
            r = requests.post(f"{BASE}{path}", timeout=180, **kw)
            if r.status_code == 200:
                return r
            print(f"  (retry {a}: HTTP {r.status_code})", flush=True)
        except Exception as e:
            print(f"  (retry {a}: {e})", flush=True)
        time.sleep(5)
    return r

r = post_retry("/auth/login", data={"username": "admin", "password": "Admin@123"})
r.raise_for_status()
H = {"Authorization": f"Bearer {r.json()['access_token']}"}
print("login ok", flush=True)

import sys
QUESTIONS = [sys.argv[1]] if len(sys.argv) > 1 else [
    "What is the purpose of the Low Pressure (Altitude) test method?",
    "Which method covers high temperature testing and what is its purpose?",
    "What is tailoring in MIL-STD-810H and why is it important?",
    "MIL-STD-810H 的振動(vibration)測試是哪一個 Method?它在測什麼?",
    "溫度衝擊(temperature shock)測試的目的是什麼?",
]

for i, q in enumerate(QUESTIONS, 1):
    t0 = time.perf_counter()
    resp = post_retry("/rag/query", json={"question": q, "top_k": 5}, headers=H)
    dt = time.perf_counter() - t0
    if resp.status_code != 200:
        print(f"\n[Q{i}] {q}\n  HTTP {resp.status_code}: {resp.text[:200]}", flush=True)
        continue
    d = resp.json()
    ans = (d.get("answer") or "").strip()
    srcs = d.get("sources") or []
    low = d.get("low_confidence")
    print(f"\n{'='*90}\n[Q{i}] {q}   ({dt:.1f}s, low_confidence={low}, sources={len(srcs)})", flush=True)
    print("ANSWER:", flush=True)
    print(textwrap.indent(textwrap.fill(ans, 100), "  "), flush=True)
    if srcs:
        s = srcs[0]
        cite = f"{s.get('title','?')} p.{s.get('page','?')}"
        snip = (s.get('text') or s.get('content') or '')[:160].replace(chr(10), ' ')
        print(f"  TOP SOURCE: {cite}  ::  {snip}", flush=True)
