"""A/B 驗證 RAG_MAX_CONTEXT_CHUNKS 6 vs 10。只讀，不改系統設定檔。

動機：實測「沙塵測試怎麼進行」時，含實際數值的第 269/275 頁有進候選池，
但被 context 預算砍掉（答案末尾出現「另有約 N 段相關內容因長度限制未展開」），
導致回答只剩「需明確指定」這類清單式內容。

用「怎麼進行 / 程序」類問題（最容易撞到 INFORMATION REQUIRED 章節）測，
指標為答案中的具體數值個數 —— 這類問題本來就該給出條件數值。

沿用固定 seed（先前實測同組重跑標準差 0.000~0.002，單次即可重現）。
"""
from __future__ import annotations

import argparse
import re
import statistics
import sys
import time

import requests

sys.path.insert(0, ".")

from app.core.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.services import ai, retrieval  # noqa: E402

OLLAMA = "http://127.0.0.1:11434"
LLM_MODEL = "qwen3:8b"

PROMPT = """你是專業的軍規測試標準查詢助理。請「只依據」以下參考資料回答問題，
不得加入資料中沒有的內容。若資料不足以回答，請明說缺什麼。用繁體中文回答。

參考資料：
{ctx}

問題：{q}

回答："""

QUESTIONS = [
    "沙塵測試怎麼進行",
    "鹽霧測試怎麼進行",
    "低溫測試怎麼進行",
    "高溫測試怎麼進行",
    "濕度測試怎麼進行",
    "振動測試怎麼進行",
    "溫度衝擊測試怎麼進行",
    "淋雨測試怎麼進行",
    "太陽輻射測試怎麼進行",
    "黴菌測試怎麼進行",
]

# 具體數值（帶單位）——這類問題的答案應該要有
NUM_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|°?[CF]|º[CF]|m/s|ft/min|g/m3|g/m³|mm|cm|kPa|hPa|小時|分鐘|英寸|inch)"
)
# 「沒給數值」的典型措辭
VAGUE = ["需明確指定", "需根據", "依測試計畫", "應由測試計畫", "未明確", "需指定", "由需求文件"]


def ask(prompt: str, seed: int = 1000) -> tuple[str, float]:
    t0 = time.perf_counter()
    r = requests.post(f"{OLLAMA}/api/chat", json={
        "model": LLM_MODEL, "messages": [{"role": "user", "content": prompt}],
        "think": False, "stream": False,
        "options": {"num_ctx": 16384, "temperature": 0.2, "seed": seed},
    }, timeout=900)
    r.raise_for_status()
    return r.json()["message"]["content"].strip(), time.perf_counter() - t0


def build_context(db, filtered) -> tuple[str, int]:
    """複製 rag.py 的 context 組裝：先取 keep_ids，再逐塊套用長度預算。"""
    keep = retrieval.context_keep_ids(filtered)
    used, parts, n = 0, [], 0
    for chunk, _ in filtered:
        if chunk.id not in keep:
            continue
        text = retrieval.context_text_budgeted(db, chunk, used)
        used += len(text)
        parts.append(f"[第{chunk.page}頁] {text}")
        n += 1
    return "\n\n".join(parts), n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-k", type=int, default=8)
    ap.add_argument("--arms", type=int, nargs=2, default=[6, 10])
    args = ap.parse_args()

    db = SessionLocal()
    orig = settings.RAG_MAX_CONTEXT_CHUNKS
    agg = {a: {"num": [], "vague": [], "sec": [], "len": [], "chunks": []} for a in args.arms}

    print(f"模型 {LLM_MODEL}（關思考）　top_k={args.top_k}　"
          f"對照 RAG_MAX_CONTEXT_CHUNKS = {args.arms[0]} vs {args.arms[1]}")
    print(f"題數 {len(QUESTIONS)}\n")

    try:
        for qi, q in enumerate(QUESTIONS, 1):
            emb = ai.embed_texts([q])[0]
            filtered = retrieval.hybrid_retrieve(db, q, emb, args.top_k)
            line = f"Q{qi:2} {q[:14]:16}"
            for arm in args.arms:
                settings.RAG_MAX_CONTEXT_CHUNKS = arm
                ctx, nch = build_context(db, filtered)
                ans, dt = ask(PROMPT.format(ctx=ctx, q=q))
                nums = len(set(NUM_RE.findall(ans)))
                vg = sum(ans.count(v) for v in VAGUE)
                agg[arm]["num"].append(nums)
                agg[arm]["vague"].append(vg)
                agg[arm]["sec"].append(dt)
                agg[arm]["len"].append(len(ans))
                agg[arm]["chunks"].append(nch)
                line += f"　{arm}: 塊{nch} 數值{nums} 含糊{vg}"
            d = agg[args.arms[1]]["num"][-1] - agg[args.arms[0]]["num"][-1]
            print(line + ("  ← 調大較好" if d > 0 else ("  ← 調小較好" if d < 0 else "")))
    finally:
        settings.RAG_MAX_CONTEXT_CHUNKS = orig
        db.close()

    print("\n" + "=" * 78)
    for arm in args.arms:
        a = agg[arm]
        print(f"  MAX_CONTEXT_CHUNKS={arm:2}：平均 context 塊數 {statistics.mean(a['chunks']):.1f}　"
              f"具體數值 {statistics.mean(a['num']):.2f}　含糊措辭 {statistics.mean(a['vague']):.2f}　"
              f"{statistics.mean(a['sec']):.1f}s　{statistics.mean(a['len']):.0f}字")
    lo, hi = args.arms
    w = sum(1 for x, y in zip(agg[lo]["num"], agg[hi]["num"]) if y > x)
    l = sum(1 for x, y in zip(agg[lo]["num"], agg[hi]["num"]) if x > y)
    print(f"\n  逐題數值豐富度：調大({hi})勝 {w}　調小({lo})勝 {l}　平手 {len(QUESTIONS)-w-l}")


if __name__ == "__main__":
    main()
