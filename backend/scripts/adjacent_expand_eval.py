"""相鄰塊擴展的擴大驗證（15 題）＋客觀指標。只讀，不改系統。

避免「挑幾題說哪個好」，這裡對每個答案自動量測：

1. 落地率(grounded ratio)：答案裡的關鍵詞（Method/Procedure 名、帶單位的數值、
   章節編號）有多少比例能在「它自己拿到的參考資料」中找到。
   ——直接抓 Method 502 那種「編出別的方法名稱」的幻覺。
2. 保留語(hedge)次數：出現「資料不足 / 需參考 / 並非 / 未提及」等字樣的次數。
3. 具體引用數：引用到 paragraph / STEP / 頁碼的次數，作為可查證性的代理指標。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time

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

# 關鍵詞：Method/Procedure 名稱、帶單位數值、章節編號
TERM_RES = [
    re.compile(r"Method\s*\d{3}(?:\.\d+)?", re.I),
    re.compile(r"Procedure\s+[IVX]+\s*[-–]\s*([A-Za-z]+)"),
    re.compile(r"-?\d+(?:\.\d+)?\s*°?\s*[CF]\b"),
    re.compile(r"\d+(?:\.\d+)?\s*(?:小時|分鐘|hours?|minutes?)"),
    re.compile(r"paragraph\s*[\d.]+", re.I),
]
CITE_RE = re.compile(r"(paragraph\s*[\d.]+|STEP\s*\d+|第\s*\d+\s*頁)", re.I)


def embed(text: str) -> np.ndarray:
    r = requests.post(f"{OLLAMA}/api/embed",
                      json={"model": EMBED_MODEL, "input": [text], "truncate": True}, timeout=180)
    r.raise_for_status()
    v = np.array(r.json()["embeddings"][0], dtype="float32")
    return v / (np.linalg.norm(v) + 1e-12)


def ask(prompt: str) -> tuple[str, float]:
    t0 = time.perf_counter()
    r = requests.post(f"{OLLAMA}/api/chat", json={
        "model": LLM_MODEL, "messages": [{"role": "user", "content": prompt}],
        "think": False, "stream": False,
        "options": {"num_ctx": 16384, "temperature": 0.2},
    }, timeout=900)
    r.raise_for_status()
    return r.json()["message"]["content"].strip(), time.perf_counter() - t0


def extract_terms(ans: str) -> list[str]:
    out: list[str] = []
    for rx in TERM_RES:
        for m in rx.finditer(ans):
            out.append(m.group(0).strip())
    # 正規化空白後去重
    return list({re.sub(r"\s+", " ", t).lower() for t in out})


def grounded_ratio(ans: str, ctx: str) -> tuple[float, list[str]]:
    """答案關鍵詞有多少能在自己的參考資料中找到。找不到的視為疑似編造。"""
    terms = extract_terms(ans)
    if not terms:
        return 1.0, []
    low = re.sub(r"\s+", " ", ctx).lower()
    missing = [t for t in terms if t not in low]
    return (len(terms) - len(missing)) / len(terms), missing


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

    print(f"語料 {len(M)} 塊　模型 {LLM_MODEL}（關思考）　top-{args.topk}　window=±{args.window}")
    print(f"題數 {min(args.limit, len(QUESTIONS))}\n")

    agg = {"A": {"g": [], "h": 0, "c": 0, "t": [], "len": []},
           "B": {"g": [], "h": 0, "c": 0, "t": [], "len": []}}
    detail = []

    for qi, q in enumerate(QUESTIONS[: args.limit], 1):
        qs = V @ embed(q)
        base = [int(i) for i in np.argsort(-qs)[: args.topk]]
        exp = set(base)
        for i in base:
            for d in range(-args.window, args.window + 1):
                j = pos.get(M[i]["idx"] + d)
                if j is not None:
                    exp.add(j)

        def ctx(idxs):
            return "\n\n".join(f"[第{M[i]['page']}頁] {M[i]['text']}" for i in sorted(idxs))

        arms = {"A": ctx(base), "B": ctx(exp)}
        row = {"q": q, "chunks": (len(base), len(exp))}
        for arm, c in arms.items():
            ans, dt = ask(PROMPT.format(ctx=c, q=q))
            g, missing = grounded_ratio(ans, c)
            h = sum(ans.count(x) for x in HEDGES)
            cites = len(CITE_RE.findall(ans))
            agg[arm]["g"].append(g); agg[arm]["h"] += h
            agg[arm]["c"] += cites; agg[arm]["t"].append(dt); agg[arm]["len"].append(len(ans))
            row[arm] = {"g": g, "h": h, "cites": cites, "missing": missing[:4], "ans": ans}
        detail.append(row)
        d = row["B"]["g"] - row["A"]["g"]
        flag = "  ← 差異大" if abs(d) >= 0.15 else ""
        print(f"Q{qi:2} 落地率 A={row['A']['g']:.2f} B={row['B']['g']:.2f} "
              f"({d:+.2f})  保留語 A={row['A']['h']} B={row['B']['h']}{flag}")

    print("\n" + "=" * 74)
    print("彙總（15 題平均）")
    for arm in ("A", "B"):
        a = agg[arm]
        print(f"  {arm} {'對照組(僅top-k)' if arm=='A' else '實驗組(+相鄰塊)'}："
              f"落地率 {np.mean(a['g']):.3f}　保留語 {a['h']} 次　"
              f"具體引用 {a['c']} 次　平均 {np.mean(a['t']):.1f}s　答案 {int(np.mean(a['len']))} 字")

    print("\n=== 疑似編造的詞（不在自己參考資料中）===")
    for i, r in enumerate(detail, 1):
        for arm in ("A", "B"):
            if r[arm]["missing"]:
                print(f"  Q{i} {arm}: {r[arm]['missing']}")

    out = "scripts/adjacent_expand_eval.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(detail, f, ensure_ascii=False, indent=2)
    print(f"\n完整答案已存：{out}")


if __name__ == "__main__":
    main()
