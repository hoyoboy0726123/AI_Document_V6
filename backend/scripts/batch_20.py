"""Simulate a test engineer: 20 varied questions through the hybrid /agent/route."""
import time, json, requests

BASE = "http://127.0.0.1:8001/api/v1"
def login():
    r = requests.post(f"{BASE}/auth/login", data={"username":"admin","password":"Admin@123"}); r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}
H = login()

# (category, question)
QS = [
 ("程序",   "高溫測試 Method 501.7 怎麼進行?有哪幾種程序?"),
 ("程序",   "振動測試 Method 514.8 的測試流程是什麼?"),
 ("程序",   "低壓(高度)測試 Method 500.6 模擬到多高的高度?"),
 ("數值",   "雨水測試 Method 506.6 的降雨率與風速條件是多少?"),
 ("數值",   "溫度衝擊測試 Method 503.7 的高低溫轉換時間要求是什麼?"),
 ("數值",   "鹽霧測試的鹽水濃度與溫度條件是多少?"),
 ("數值",   "MIL-HDBK-310 的低溫設計值採用多少百分比的時數?"),
 ("關係",   "鹽霧測試 509.7 引用了哪些外部規範?"),
 ("關係",   "沙塵測試 510.7 參考哪些標準?"),
 ("關係",   "MIL-HDBK-310 跟 MIL-STD-810、MIL-STD-210 之間是什麼關係?"),
 ("關係",   "振動測試 Method 514.8 引用了哪些規範?"),
 ("關係",   "MIL-PRF-7808 這個潤滑油規範被哪些文件引用?"),
 ("列舉",   "MIL-STD-810H 有哪些測試方法?"),
 ("列舉",   "沙塵測試有哪些子程序?"),
 ("列舉",   "衝擊測試 Method 516.8 有哪些程序?"),
 ("版本",   "MIL-STD-810 有哪些版本演進?"),
 ("版本",   "MIL-STD-210B 被什麼取代了?"),
 ("比較",   "鹽霧測試和酸性大氣測試(509.7 vs 518.2)有什麼差別?"),
 ("應用",   "我的產品要用在沙漠高溫多塵環境,該做哪些 810H 測試?各參考什麼規範?"),
 ("應用",   "低溫測試 502.7 引用的氣候數據來自哪份文件?該文件的低溫數值怎麼定義?"),
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
    print(f"Q{i:>2} [{cat}] route={mode} tools={tools} {dt}s", flush=True)
    print(f"   {q}", flush=True)
    print("   ---", flush=True)
    print("   " + "\n   ".join(final[:900].splitlines()), flush=True)

print("\n=== batch 20 done ===", flush=True)
