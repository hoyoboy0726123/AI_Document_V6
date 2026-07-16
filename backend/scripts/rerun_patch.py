"""Re-run specific question indices and patch them back into qa_results.jsonl (by idx)."""
import sys, json, os, time, requests

BASE = "http://127.0.0.1:8001/api/v1"
SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qa_results.jsonl")
TARGET = set(int(x) for x in sys.argv[1:]) if len(sys.argv) > 1 else set()

def login():
    r = requests.post(f"{BASE}/auth/login", data={"username":"admin","password":"Admin@123"}); r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}
H = login()

recs = [json.loads(l) for l in open(SRC, encoding="utf-8") if l.strip()]

def ask(q):
    mode, tools, final = None, 0, ""
    with requests.post(f"{BASE}/agent/route", json={"question": q, "max_steps": 8}, headers=H, stream=True, timeout=400) as resp:
        ev = None
        for line in resp.iter_lines(decode_unicode=True):
            if not line: continue
            if line.startswith("event:"): ev = line.split(":",1)[1].strip()
            elif line.startswith("data:"):
                d = json.loads(line.split(":",1)[1].strip())
                if ev == "route": mode = d.get("mode")
                elif ev == "tool_call": tools += 1
                elif ev == "final": final = d.get("text") or ""
                elif ev == "error": final = "ERROR: " + str(d.get("message"))
    return mode, tools, final

for rec in recs:
    if rec["idx"] not in TARGET:
        continue
    t0 = time.perf_counter()
    mode, tools, final = ask(rec["question"])
    rec["route"], rec["tools"], rec["seconds"], rec["answer"] = mode, tools, int(time.perf_counter()-t0), final
    print(f"[{rec['idx']}] route={mode} tools={tools} len={len(final)}\n   {final[:160]}\n", flush=True)

with open(SRC, "w", encoding="utf-8") as f:
    for rec in recs:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
print("patched", sorted(TARGET))
