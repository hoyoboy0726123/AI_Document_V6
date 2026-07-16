"""Run all 42 questions (round1 + round2) through hybrid /agent/route, capture FULL answers,
write each record as JSONL → backend/scripts/qa_results.jsonl (for Excel export)."""
import time, json, os, requests

BASE = "http://127.0.0.1:8001/api/v1"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qa_results.jsonl")

def login():
    r = requests.post(f"{BASE}/auth/login", data={"username":"admin","password":"Admin@123"}); r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}
H = login()

QS = [
 (1,"程序","高溫測試 Method 501.7 怎麼進行?有哪幾種程序?"),
 (1,"程序","振動測試 Method 514.8 的測試流程是什麼?"),
 (1,"程序","低壓(高度)測試 Method 500.6 模擬到多高的高度?"),
 (1,"數值","雨水測試 Method 506.6 的降雨率與風速條件是多少?"),
 (1,"數值","溫度衝擊測試 Method 503.7 的高低溫轉換時間要求是什麼?"),
 (1,"數值","鹽霧測試的鹽水濃度與溫度條件是多少?"),
 (1,"數值","MIL-HDBK-310 的低溫設計值採用多少百分比的時數?"),
 (1,"關係","鹽霧測試 509.7 引用了哪些外部規範?"),
 (1,"關係","沙塵測試 510.7 參考哪些標準?"),
 (1,"關係","MIL-HDBK-310 跟 MIL-STD-810、MIL-STD-210 之間是什麼關係?"),
 (1,"關係","振動測試 Method 514.8 引用了哪些規範?"),
 (1,"關係","MIL-PRF-7808 這個潤滑油規範被哪些文件引用?"),
 (1,"列舉","MIL-STD-810H 有哪些測試方法?"),
 (1,"列舉","沙塵測試有哪些子程序?"),
 (1,"列舉","衝擊測試 Method 516.8 有哪些程序?"),
 (1,"版本","MIL-STD-810 有哪些版本演進?"),
 (1,"版本","MIL-STD-210B 被什麼取代了?"),
 (1,"比較","鹽霧測試和酸性大氣測試(509.7 vs 518.2)有什麼差別?"),
 (1,"應用","我的產品要用在沙漠高溫多塵環境,該做哪些 810H 測試?各參考什麼規範?"),
 (1,"應用","低溫測試 502.7 引用的氣候數據來自哪份文件?該文件的低溫數值怎麼定義?"),
 (2,"程序","浸水測試 Method 512.6 怎麼進行?水深與時間要求?"),
 (2,"程序","加速度測試 Method 513.8 的測試程序與方向?"),
 (2,"程序","爆炸性大氣測試 Method 511.7 的測試條件是什麼?"),
 (2,"程序","結冰/凍雨測試 Method 521.4 怎麼做?結冰厚度?"),
 (2,"程序","衝擊測試 Method 516.8 的衝擊波形與峰值加速度?"),
 (2,"程序","噪音測試 Method 515.8 的聲壓級(SPL)要求?"),
 (2,"程序","太陽輻射測試 Method 505.7 的輻射強度與循環?"),
 (2,"數值","黴菌測試 Method 508.8 使用哪些黴菌菌種?溫濕度條件?"),
 (2,"數值","鹽霧測試要做多久?暴露時間與週期?"),
 (2,"數值","綜合環境測試 Method 520.5 結合了哪些環境?"),
 (2,"關係","MIL-STD-461 是規範什麼的?它引用哪些標準?"),
 (2,"關係","MIL-DTL-901 衝擊規範被哪些 810H 方法引用?"),
 (2,"關係","ASTM B117 被哪些 810H 測試方法引用?"),
 (2,"關係","MIL-STD-882 是什麼規範?跟 810H 什麼關係?"),
 (2,"版本","MIL-STD-704 有哪些版本?最新是哪個?"),
 (2,"版本","MIL-PRF-23699 這個潤滑油有哪些版本?引用了什麼?"),
 (2,"列舉","振動測試 Method 514.8 有哪些子章節?"),
 (2,"列舉","槍砲衝擊測試 Method 519.8 有哪些程序?"),
 (2,"應用","我的設備要裝在軍艦上,該做哪些 810H 振動與衝擊測試?參考什麼規範?"),
 (2,"應用","航空渦輪引擎潤滑油要符合哪個 MIL 規範?有哪些等級?"),
 (2,"誠實","MIL-STD-810H 有規範鋰電池安全測試嗎?是哪個方法?"),
 (2,"誠實","電磁干擾(EMI/EMC)測試在 810H 是哪個方法?還是要看別的規範?"),
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

with open(OUT, "w", encoding="utf-8") as f:
    pass
for i, (rnd, cat, q) in enumerate(QS, 1):
    t0 = time.perf_counter()
    try:
        mode, tools, final = ask(q)
    except Exception as e:
        mode, tools, final = "EXC", 0, "ERROR: " + str(e)[:120]
    dt = int(time.perf_counter()-t0)
    rec = {"idx": i, "round": rnd, "category": cat, "question": q,
           "route": mode, "tools": tools, "seconds": dt, "answer": final}
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"[{i:>2}/42] R{rnd} {cat} route={mode} tools={tools} {dt}s  len={len(final)}", flush=True)

print(f"\n=== batch_all done → {OUT} ===", flush=True)
