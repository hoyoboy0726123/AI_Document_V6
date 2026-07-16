"""Test whether gemma4:12b thinking can be disabled (think param / template) and how fast it gets."""
import time, requests

PROMPT = ("你是文件問答助理,只能根據以下段落作答,用繁體中文簡潔回答。\n"
          "段落:低壓(高度)試驗用來判定器材能否在低氣壓環境中運作或耐受快速壓力變化,"
          "適用於高海拔儲存/操作、加壓或非加壓空運的器材。\n\n問題:低壓(高度)試驗的目的是什麼?")

def run(label, body):
    t0 = time.perf_counter()
    r = requests.post("http://127.0.0.1:11434/api/chat", json=body, timeout=600)
    dt = time.perf_counter() - t0
    m = r.json().get("message", {})
    c = m.get("content", ""); th = m.get("thinking", "") or ""
    print(f"[{label}] {dt:6.1f}s  content={len(c)}  thinking={len(th)}", flush=True)
    print("   ->", c[:160].replace(chr(10), " "), flush=True)

base = {"model": "gemma4:12b", "messages": [{"role": "user", "content": PROMPT}], "stream": False}
run("default", dict(base))
run("think=false", dict(base, think=False))
run("think=false+/no_think", dict(base, think=False,
    messages=[{"role": "user", "content": PROMPT + "\n/no_think"}]))
