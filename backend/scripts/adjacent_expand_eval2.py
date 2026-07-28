"""相鄰塊擴展 — 嚴謹驗證版。只讀，不改系統。

相對前一版修正三件事：
1. 落地率比對正規化：統一破折號、去除度數符號與單位前後空白、中英單位對照
   （上一版把「Procedure III – Manipulation」誤判為編造，只因用了 en-dash）。
2. 每題每組跑 N 次：實測顯示同題兩次會給出不同答案，單次結果不可靠。
3. 輸出差異最大的題目答案，供人工抽驗。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import statistics
import time
import unicodedata

import numpy as np
import requests

OLLAMA = "http://127.0.0.1:11434"
EMBED_MODEL = "qwen3-embedding:8b"
LLM_MODEL = "qwen3:8b"

PROMPT = """你是專業的軍規測試標準查詢助理。請「只依據」以下參考資料回答問題，
不得加入資料中沒有的內容。若資料不足以回答，請明說缺什麼。用繁體中文回答。

參考資料：
{ctx}

問題：{q}

回答："""

HEDGES = ["資料不足", "未提及", "需參考", "並非", "無法確定", "沒有提供", "未明確"]
CITE_RE = re.compile(r"(paragraph\s*[\d.]+|STEP\s*\d+|第\s*\d+\s*頁)", re.I)

# 中文單位 → 英文，讓「48 小時」能對上原文的「48 hours」
UNIT_MAP = {"小時": "hour", "分鐘": "minute", "秒": "second", "天": "day"}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("℃", "°C").replace("℉", "°F")
    s = re.sub(r"[‐-―−]", "-", s)      # 各式破折號統一
    for zh, en in UNIT_MAP.items():
        s = s.replace(zh, en)
    s = re.sub(r"\s*-\s*", "-", s)                     # 破折號前後空白
    s = re.sub(r"\s*°\s*", "°", s)                     # 度數符號前後空白
    s = re.sub(r"(\d)\s+(?=[a-zA-Z°])", r"\1", s)      # 數字與單位間空白
    s = re.sub(r"\s+", " ", s)
    return s.lower()


NAME_RE = [
    re.compile(r"Method\s*\d{3}(?:\.\d+)?", re.I),
    # 注意：比對前文字已被 norm() 轉小寫，這裡必須加 re.I，否則永遠匹配不到
    re.compile(r"Procedure\s+[IVX]+\s*[-‐-―]\s*[A-Za-z]+", re.I),
    re.compile(r"paragraph\s*[\d.]+", re.I),
]
NUM_RE = re.compile(r"-?\d+(?:\.\d+)?\s*(?:°[CF]|hour|minute|second|day)s?", re.I)


def grounded(ans: str, ctx: str) -> tuple[float | None, list[str]]:
    """名稱類用正規化後子字串比對；數值類額外放寬為「數字本身有出現」。"""
    a, c = norm(ans), norm(ctx)
    terms = {m.group(0).strip() for rx in NAME_RE for m in rx.finditer(a)}
    nums = {m.group(0).strip() for m in NUM_RE.finditer(a)}
    checked, missing = 0, []
    for t in terms:
        checked += 1
        if t not in c:
            missing.append(t)
    for t in nums:
        checked += 1
        if t in c:
            continue
        digits = re.match(r"-?\d+(?:\.\d+)?", t)
        # 放寬：數值本身有出現在原文即視為有依據（單位換算/翻譯差異不算編造）
        if digits and re.search(rf"(?<!\d){re.escape(digits.group(0))}(?!\d)", c):
            continue
        missing.append(t)
    if checked == 0:
        return None, []
    return (checked - len(missing)) / checked, missing


def embed(text: str) -> np.ndarray:
    r = requests.post(f"{OLLAMA}/api/embed",
                      json={"model": EMBED_MODEL, "input": [text], "truncate": True}, timeout=180)
    r.raise_for_status()
    v = np.array(r.json()["embeddings"][0], dtype="float32")
    return v / (np.linalg.norm(v) + 1e-12)


def ask(prompt: str, seed: int) -> tuple[str, float]:
    t0 = time.perf_counter()
    r = requests.post(f"{OLLAMA}/api/chat", json={
        "model": LLM_MODEL, "messages": [{"role": "user", "content": prompt}],
        "think": False, "stream": False,
        "options": {"num_ctx": 16384, "temperature": 0.2, "seed": seed},
    }, timeout=900)
    r.raise_for_status()
    return r.json()["message"]["content"].strip(), time.perf_counter() - t0


QUESTIONS = [
    "低溫測試 Method 502 的試驗程序有哪幾個?請依序說明",
    "高溫測試的溫度穩定與持續時間要求是什麼",
    "溫度衝擊測試 Method 503 的程序與轉換要求為何",
    "振動測試 Method 514 包含哪些程序",
    "濕度測試 Method 507 的溫濕度循環條件為何",
    "鹽霧測試 Method 509 的鹽溶液濃度與暴露時間要求",
    "砂塵測試 Method 510 分為哪幾種程序?各自目的為何",
    "低壓(高度)測試 Method 500 有哪些程序",
    "衝擊測試 Method 516 的功能性衝擊要求為何",
    "淋雨測試 Method 506 的降雨率與風速要求",
    "爆炸性大氣測試 Method 511 的適用範圍與目的",
    "測試項目在測試前後需要進行哪些檢查",
    "如何決定多項環境測試的執行順序",
    "溫度測試中「溫度穩定」的定義是什麼",
    "測試過程中若試品失效，應依照什麼程序處理",
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="doc_management.db")
    ap.add_argument("--title-like", default="%810H%")
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--window", type=int, default=1)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--limit", type=int, default=len(QUESTIONS))
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    rows = con.execute(
        """select ch.chunk_index, ch.page, ch.text, ch.embedding
           from document_chunks ch join documents d on d.id = ch.document_id
           where d.title like ? and ch.embedding is not null order by ch.chunk_index""",
        (args.title_like,)).fetchall()
    con.close()

    V = np.array([json.loads(r[3]) for r in rows], dtype="float32")
    V /= np.linalg.norm(V, axis=1, keepdims=True) + 1e-12
    M = [{"idx": r[0], "page": r[1], "text": r[2]} for r in rows]
    pos = {m["idx"]: i for i, m in enumerate(M)}

    qs_list = QUESTIONS[: args.limit]
    total = len(qs_list) * 2 * args.runs
    print(f"語料 {len(M)} 塊　{LLM_MODEL}（關思考）　top-{args.topk}　window=±{args.window}")
    print(f"題數 {len(qs_list)}　每組跑 {args.runs} 次　共 {total} 次生成\n")

    detail = []
    for qi, q in enumerate(qs_list, 1):
        sims = V @ embed(q)
        base = [int(i) for i in np.argsort(-sims)[: args.topk]]
        exp = set(base)
        for i in base:
            for d in range(-args.window, args.window + 1):
                j = pos.get(M[i]["idx"] + d)
                if j is not None:
                    exp.add(j)

        def ctx(idxs):
            return "\n\n".join(f"[第{M[i]['page']}頁] {M[i]['text']}" for i in sorted(idxs))

        arms = {"A": ctx(base), "B": ctx(exp)}
        rec: dict = {"q": q, "chunks": {"A": len(base), "B": len(exp)}, "runs": {"A": [], "B": []}}
        for arm, c in arms.items():
            for run in range(args.runs):
                ans, dt = ask(PROMPT.format(ctx=c, q=q), seed=1000 + run)
                g, miss = grounded(ans, c)
                rec["runs"][arm].append({
                    "g": g, "missing": miss, "hedge": sum(ans.count(x) for x in HEDGES),
                    "cites": len(CITE_RE.findall(ans)), "sec": dt, "len": len(ans), "ans": ans,
                })
        detail.append(rec)

        def agg(arm, key):
            vals = [r[key] for r in rec["runs"][arm] if r[key] is not None]
            return statistics.mean(vals) if vals else float("nan")

        ga, gb = agg("A", "g"), agg("B", "g")
        print(f"Q{qi:2} 落地率 A={ga:.2f} B={gb:.2f} ({gb-ga:+.2f})　"
              f"引用 A={agg('A','cites'):.1f} B={agg('B','cites'):.1f}　"
              f"保留語 A={agg('A','hedge'):.1f} B={agg('B','hedge'):.1f}")

    # 彙總
    print("\n" + "=" * 76)
    print(f"彙總（{len(qs_list)} 題 × {args.runs} 次）")
    summary = {}
    for arm in ("A", "B"):
        flat = [r for rec in detail for r in rec["runs"][arm]]
        gs = [r["g"] for r in flat if r["g"] is not None]
        summary[arm] = {
            "grounded_mean": statistics.mean(gs), "grounded_sd": statistics.pstdev(gs),
            "cites": statistics.mean([r["cites"] for r in flat]),
            "hedge": statistics.mean([r["hedge"] for r in flat]),
            "sec": statistics.mean([r["sec"] for r in flat]),
            "len": statistics.mean([r["len"] for r in flat]),
        }
        s = summary[arm]
        label = "對照組(僅top-k)" if arm == "A" else "實驗組(+相鄰塊)"
        print(f"  {arm} {label}：落地率 {s['grounded_mean']:.3f}±{s['grounded_sd']:.3f}　"
              f"引用 {s['cites']:.2f}　保留語 {s['hedge']:.2f}　{s['sec']:.1f}s　{s['len']:.0f}字")

    # 逐題勝負（以落地率為準，差距 <0.05 視為平手）
    win = {"A": 0, "B": 0, "tie": 0}
    for rec in detail:
        ga = statistics.mean([r["g"] for r in rec["runs"]["A"] if r["g"] is not None] or [0])
        gb = statistics.mean([r["g"] for r in rec["runs"]["B"] if r["g"] is not None] or [0])
        win["B" if gb - ga > 0.05 else "A" if ga - gb > 0.05 else "tie"] += 1
    print(f"\n  逐題落地率勝負：B 勝 {win['B']}　A 勝 {win['A']}　平手 {win['tie']}")

    # 同組三次之間的變異（衡量結果可信度）
    print("\n  同組重跑變異（落地率標準差，越小越穩定）：")
    for arm in ("A", "B"):
        sds = []
        for rec in detail:
            gs = [r["g"] for r in rec["runs"][arm] if r["g"] is not None]
            if len(gs) > 1:
                sds.append(statistics.pstdev(gs))
        print(f"    {arm}: {statistics.mean(sds):.3f}")

    print("\n=== 仍疑似編造（人工抽驗用）===")
    for i, rec in enumerate(detail, 1):
        for arm in ("A", "B"):
            miss = sorted({m for r in rec["runs"][arm] for m in r["missing"]})
            if miss:
                print(f"  Q{i} {arm}: {miss[:6]}")

    with open("scripts/eval2_detail.json", "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "win": win, "detail": detail}, f, ensure_ascii=False, indent=2)
    print("\n完整答案已存：scripts/eval2_detail.json")


if __name__ == "__main__":
    main()
