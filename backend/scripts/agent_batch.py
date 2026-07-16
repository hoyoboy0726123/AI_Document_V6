"""Batch-test the ReAct Agent: for each question show tools called + final answer + time."""
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

QUESTIONS = [
    "MIL-STD-810H 取代了哪一個版本?它的版本演進鏈(supersedes/derives)為何?",
    "MIL-STD-810H 強制引用(requires)了哪些 ASTM 與 ISO 標準?",
    "鹽霧(salt fog)測試是哪一個 Method?測試程序大致為何?",
    "MIL-STD-810H 跟 MIL-STD-167 之間有什麼關聯?",
    "MIL-STD-810H 對量子電腦的抗磁暴測試方法是哪一個 Method?",
]

def ask(q):
    tools, thoughts, final = [], 0, ""
    t0 = time.perf_counter()
    with requests.post(f"{BASE}/agent/chat", json={"question": q}, headers=H, stream=True, timeout=600) as r:
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
                if event == "thought":
                    thoughts += 1
                elif event == "tool_call":
                    tools.append(f"{d.get('action') or d.get('tool')}({json.dumps(d.get('action_input') or d.get('input') or {}, ensure_ascii=False)})")
                elif event == "final":
                    final = d.get("text", "")
    return time.perf_counter() - t0, thoughts, tools, final

for i, q in enumerate(QUESTIONS, 1):
    dt, th, tools, final = ask(q)
    print("=" * 95, flush=True)
    print(f"[Q{i}] {q}", flush=True)
    print(f"  ⏱ {dt:.1f}s | thoughts={th} | tool_calls={len(tools)}", flush=True)
    for t in tools:
        print(f"    🔧 {t}", flush=True)
    print("  ✅ ANSWER:", flush=True)
    for line in final.strip().splitlines():
        if line.strip():
            print(f"     {line.strip()[:140]}", flush=True)
    print(flush=True)
