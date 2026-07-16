"""Test the hybrid /agent/route endpoint: shows which mode was picked + the final answer head."""
import sys, json, requests

BASE = "http://127.0.0.1:8001/api/v1"
QS = sys.argv[1:] or [
    "MIL-HDBK-310 提供哪些氣候設計數值?低溫設計數據是多少?",   # content -> rag
    "MIL-HDBK-310 引用了哪些其他標準?跟 MIL-STD-810 什麼關係?",  # relation -> agent
]

r = requests.post(f"{BASE}/auth/login", data={"username": "admin", "password": "Admin@123"})
H = {"Authorization": f"Bearer {r.json()['access_token']}"}

for q in QS:
    print("\n" + "=" * 90 + f"\nQ: {q}", flush=True)
    mode, final, ntools = None, "", 0
    with requests.post(f"{BASE}/agent/route", json={"question": q, "max_steps": 8},
                       headers=H, stream=True, timeout=400) as resp:
        ev = None
        for line in resp.iter_lines(decode_unicode=True):
            if not line:
                continue
            if line.startswith("event:"):
                ev = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = json.loads(line.split(":", 1)[1].strip())
                if ev == "route":
                    mode = data.get("mode"); print(f"  >> ROUTED TO: {mode}", flush=True)
                elif ev == "tool_call":
                    ntools += 1
                elif ev == "final":
                    final = data.get("text") or ""
                elif ev == "error":
                    final = "ERROR: " + str(data.get("message"))
    print(f"  mode={mode} tool_calls={ntools}\n  ANSWER head:\n  " +
          "\n  ".join(final[:700].splitlines()), flush=True)
