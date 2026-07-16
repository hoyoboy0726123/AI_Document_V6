"""Drive the ReAct Agent (which uses KG tools) over SSE and print each reasoning step."""
import json, time, requests

BASE = "http://127.0.0.1:8001/api/v1"

def login():
    for _ in range(6):
        try:
            r = requests.post(f"{BASE}/auth/login", data={"username": "admin", "password": "Admin@123"}, timeout=15)
            if r.status_code == 200:
                return r.json()["access_token"]
        except Exception:
            pass
        time.sleep(3)
    raise SystemExit("login failed")

H = {"Authorization": f"Bearer {login()}"}
print("login ok\n", flush=True)

import sys
Q = sys.argv[1] if len(sys.argv) > 1 else (
    "MIL-STD-810H 是從哪些舊版本演進而來的(derives_from / supersedes)?"
    "另外,它強制引用(requires)了哪些 ASTM 標準?請依據規範關係作答。")
print(f"QUESTION: {Q}\n{'='*90}", flush=True)

t0 = time.perf_counter()
with requests.post(f"{BASE}/agent/chat", json={"question": Q}, headers=H, stream=True, timeout=600) as r:
    event = None
    for raw in r.iter_lines(decode_unicode=True):
        if raw is None or raw == "":
            continue
        if raw.startswith("event:"):
            event = raw[6:].strip()
        elif raw.startswith("data:"):
            data = raw[5:].strip()
            try:
                d = json.loads(data)
            except Exception:
                d = {"raw": data}
            el = time.perf_counter() - t0
            if event == "thought":
                print(f"[{el:5.1f}s] 💭 THOUGHT: {str(d.get('text') or d)[:240]}", flush=True)
            elif event == "tool_call":
                print(f"[{el:5.1f}s] 🔧 TOOL: {d.get('action') or d.get('tool')} input={d.get('action_input') or d.get('input')}", flush=True)
            elif event == "observation":
                print(f"[{el:5.1f}s] 👁  OBS: {str(d.get('text') or d)[:300]}", flush=True)
            elif event == "final":
                print(f"\n[{el:5.1f}s] ✅ FINAL ANSWER:\n{d.get('text','')}", flush=True)
                srcs = d.get("sources") or []
                if srcs:
                    print(f"\nsources: {len(srcs)}", flush=True)
            elif event == "error":
                print(f"[{el:5.1f}s] ❌ ERROR: {d.get('message')}", flush=True)
            elif event == "done":
                print(f"\n[{el:5.1f}s] done.", flush=True)
