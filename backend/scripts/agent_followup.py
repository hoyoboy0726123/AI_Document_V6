"""Simulate a follow-up turn: user already got the sand/dust answer with referenced
standards, now asks ABOUT those referenced standards. Tests whether the agent handles
'standard referenced but its content not ingested' honestly."""
import json, time, sys, requests

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

# Prior turn (what the user just saw) → conversation history
prior_q = "濕度(humidity)測試怎麼進行?是哪一個 Method?參考/引用了哪些規範?"
prior_a = ("METHOD 507.6。引用的規範:AR 70-38、MIL-HDBK-310、NATO STANAG 4370 (AECTP 230)、"
           "MIL-STD-210B、MIL-STD-810。")
history = [{"question": prior_q, "answer": prior_a}]

followup = sys.argv[1] if len(sys.argv) > 1 else \
    ("上面提到的 AR 70-38、MIL-HDBK-310、NATO STANAG 4370、MIL-STD-210B、MIL-STD-810 這些規範,"
     "有沒有實際的測試條件內容(數值、程序)可以查?")

print(f"[FOLLOW-UP] {followup}\n{'='*90}", flush=True)
t0 = time.perf_counter()
with requests.post(f"{BASE}/agent/chat", json={"question": followup, "conversation_history": history},
                   headers=H, stream=True, timeout=600) as r:
    event = None
    for raw in r.iter_lines(decode_unicode=True):
        if not raw:
            continue
        if raw.startswith("event:"):
            event = raw[6:].strip()
        elif raw.startswith("data:"):
            try:
                d = json.loads(raw[5:].strip())
            except Exception:
                d = {}
            el = time.perf_counter() - t0
            if event == "thought":
                print(f"[{el:5.1f}s] 💭 {str(d.get('text') or '')[:200]}", flush=True)
            elif event == "tool_call":
                print(f"[{el:5.1f}s] 🔧 {d.get('tool')} {d.get('input')}", flush=True)
            elif event == "observation":
                print(f"[{el:5.1f}s] 👁  {str(d.get('output'))[:240]}", flush=True)
            elif event == "final":
                print(f"\n[{el:5.1f}s] ✅ FINAL:\n{d.get('text','')}", flush=True)
            elif event == "error":
                print(f"[{el:5.1f}s] ❌ {d.get('message')}", flush=True)
