"""答案層 A/B：檢索到的 top-k，加不加「相鄰塊」對最終答案的差異。只讀，不改系統。

對照組 A：只餵 top-k 塊（現行行為）
實驗組 B：top-k 塊 + 每塊的前後各 N 塊（補回被切斷的上下文）

兩組用同一個模型、同一份提示詞，唯一變因是上下文。
"""
from __future__ import annotations

import argparse
import json
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


def embed(text: str) -> np.ndarray:
    r = requests.post(
        f"{OLLAMA}/api/embed",
        json={"model": EMBED_MODEL, "input": [text], "truncate": True},
        timeout=180,
    )
    r.raise_for_status()
    v = np.array(r.json()["embeddings"][0], dtype="float32")
    return v / (np.linalg.norm(v) + 1e-12)


def ask(prompt: str) -> tuple[str, float]:
    t0 = time.perf_counter()
    r = requests.post(
        f"{OLLAMA}/api/chat",
        json={
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "think": False,
            "stream": False,
            "options": {"num_ctx": 16384, "temperature": 0.2},
        },
        timeout=900,
    )
    r.raise_for_status()
    return r.json()["message"]["content"].strip(), time.perf_counter() - t0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="doc_management.db")
    ap.add_argument("--title-like", default="%810H%")
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--window", type=int, default=1, help="前後各取幾塊")
    ap.add_argument("--questions", nargs="+", required=True)
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    rows = con.execute(
        """select ch.chunk_index, ch.page, ch.text, ch.embedding
           from document_chunks ch join documents d on d.id = ch.document_id
           where d.title like ? and ch.embedding is not null
           order by ch.chunk_index""",
        (args.title_like,),
    ).fetchall()
    con.close()

    V = np.array([json.loads(r[3]) for r in rows], dtype="float32")
    V /= np.linalg.norm(V, axis=1, keepdims=True) + 1e-12
    M = [{"idx": r[0], "page": r[1], "text": r[2]} for r in rows]
    pos = {m["idx"]: i for i, m in enumerate(M)}
    print(f"語料：{len(M)} 塊　模型：{LLM_MODEL}（關思考）　window=±{args.window}\n")

    for qi, q in enumerate(args.questions, 1):
        qs = V @ embed(q)
        base = list(np.argsort(-qs)[: args.topk])

        # 相鄰塊擴展：把每個命中的前後 window 塊補進來，依原文順序排列
        exp = set(int(i) for i in base)
        for i in base:
            for d in range(-args.window, args.window + 1):
                j = pos.get(M[int(i)]["idx"] + d)
                if j is not None:
                    exp.add(j)
        base_sorted = sorted(int(i) for i in base)
        exp_sorted = sorted(exp)

        def ctx(idxs):
            return "\n\n".join(
                f"[第{M[i]['page']}頁] {M[i]['text']}" for i in idxs
            )

        ca, cb = ctx(base_sorted), ctx(exp_sorted)
        print("=" * 78)
        print(f"問題 {qi}：{q}")
        print(f"  A 對照組：{len(base_sorted)} 塊 / {len(ca):,} 字")
        print(f"  B 實驗組：{len(exp_sorted)} 塊 / {len(cb):,} 字（多 {len(exp_sorted)-len(base_sorted)} 塊）")

        for name, c in (("A 對照組（僅 top-k）", ca), ("B 實驗組（+相鄰塊）", cb)):
            ans, dt = ask(PROMPT.format(ctx=c, q=q))
            print(f"\n--- {name}　{dt:.0f}s ---")
            print(ans[:1400])
        print()


if __name__ == "__main__":
    main()
