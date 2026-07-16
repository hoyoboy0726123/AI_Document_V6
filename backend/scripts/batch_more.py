"""Round 2: 22 more varied test-engineer questions through the hybrid /agent/route."""
import time, json, requests

BASE = "http://127.0.0.1:8001/api/v1"
def login():
    r = requests.post(f"{BASE}/auth/login", data={"username":"admin","password":"Admin@123"}); r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}
H = login()

QS = [
 ("程序",   "浸水測試 Method 512.6 怎麼進行?水深與時間要求?"),
 ("程序",   "加速度測試 Method 513.8 的測試程序與方向?"),
 ("程序",   "爆炸性大氣測試 Method 511.7 的測試條件是什麼?"),
 ("程序",   "結冰/凍雨測試 Method 521.4 怎麼做?結冰厚度?"),
 ("程序",   "衝擊測試 Method 516.8 的衝擊波形與峰值加速度?"),
 ("程序",   "噪音測試 Method 515.8 的聲壓級(SPL)要求?"),
 ("程序",   "太陽輻射測試 Method 505.7 的輻射強度與循環?"),
 ("數值",   "黴菌測試 Method 508.8 使用哪些黴菌菌種?溫濕度條件?"),
 ("數值",   "鹽霧測試要做多久?暴露時間與週期?"),
 ("數值",   "綜合環境測試 Method 520.5 結合了哪些環境?"),
 ("關係",   "MIL-STD-461 是規範什麼的?它引用哪些標準?"),
 ("關係",   "MIL-DTL-901 衝擊規範被哪些 810H 方法引用?"),
 ("關係",   "ASTM B117 被哪些 810H 測試方法引用?"),
 ("關係",   "MIL-STD-882 是什麼規範?跟 810H 什麼關係?"),
 ("版本",   "MIL-STD-704 有哪些版本?最新是哪個?"),
 ("版本",   "MIL-PRF-23699 這個潤滑油有哪些版本?引用了什麼?"),
 ("列舉",   "振動測試 Method 514.8 有哪些子章節?"),
 ("列舉",   "槍砲衝擊測試 Method 519.8 有哪些程序?"),
 ("應用",   "我的設備要裝在軍艦上,該做哪些 810H 振動與衝擊測試?參考什麼規範?"),
 ("應用",   "航空渦輪引擎潤滑油要符合哪個 MIL 規範?有哪些等級?"),
 ("誠實",   "MIL-STD-810H 有規範鋰電池安全測試嗎?是哪個方法?"),
 ("誠實",   "電磁干擾(EMI/EMC)測試在 810H 是哪個方法?還是要看別的規範?"),
]

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

for i, (cat, q) in enumerate(QS, 1):
    t0 = time.perf_counter()
    try:
        mode, tools, final = ask(q)
    except Exception as e:
        mode, tools, final = "EXC", 0, str(e)[:100]
    dt = int(time.perf_counter()-t0)
    print("="*100, flush=True)
    print(f"R{i:>2} [{cat}] route={mode} tools={tools} {dt}s", flush=True)
    print(f"   {q}", flush=True)
    print("   ---", flush=True)
    print("   " + "\n   ".join(final[:850].splitlines()), flush=True)

print("\n=== batch more done ===", flush=True)
