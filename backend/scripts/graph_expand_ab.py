"""A/B 驗證：檢索加不加「相似度圖擴展」的差異。只讀，不改動系統。

對照組（Baseline）：一般向量檢索，取 top-k。
實驗組（Expanded）：先取 top-k，再沿相似度圖把每個命中的「鄰居段落」拉進候選池，
                    重新依與問題的相似度排序後取 top-k。

要看的是：擴展後有沒有撈到「原本漏掉但其實相關」的段落。若兩邊撈到的幾乎一樣，
代表這個機制對目前的語料沒有作用。
"""
from __future__ import annotations

import argparse
import json
import sqlite3

import numpy as np
import requests

OLLAMA = "http://127.0.0.1:11434"
EMBED_MODEL = "qwen3-embedding:8b"


def embed_query(q: str) -> np.ndarray:
    r = requests.post(
        f"{OLLAMA}/api/embed",
        json={"model": EMBED_MODEL, "input": [q], "truncate": True},
        timeout=120,
    )
    r.raise_for_status()
    v = np.array(r.json()["embeddings"][0], dtype="float32")
    return v / (np.linalg.norm(v) + 1e-12)


def load(con: sqlite3.Connection, title_like: str | None):
    sql = """select ch.id, ch.chunk_index, ch.page, ch.text, ch.embedding, d.title
             from document_chunks ch join documents d on d.id = ch.document_id
             where ch.embedding is not null"""
    args: tuple = ()
    if title_like:
        sql += " and d.title like ?"
        args = (title_like,)
    rows = con.execute(sql, args).fetchall()
    vecs = np.array([json.loads(r[4]) for r in rows], dtype="float32")
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-12
    meta = [
        {"idx": r[1], "page": r[2], "text": r[3], "doc": r[5]} for r in rows
    ]
    return meta, vecs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="doc_management.db")
    ap.add_argument("--question", required=True)
    ap.add_argument("--title-like", default=None, help="限定文件；不給則全庫")
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--edge-threshold", type=float, default=0.6)
    ap.add_argument("--neighbors", type=int, default=3, help="每個命中拉幾個鄰居")
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    meta, vecs = load(con, args.title_like)
    con.close()
    print(f"語料：{len(meta)} 塊" + (f"（限定 {args.title_like}）" if args.title_like else "（全庫）"))

    q = embed_query(args.question)
    qsim = vecs @ q

    base_idx = np.argsort(-qsim)[: args.topk]

    # 沿相似度圖擴展：把命中段落的高相似鄰居也拉進候選
    cand = set(int(i) for i in base_idx)
    added_by = {}
    for i in base_idx:
        sims = vecs @ vecs[i]
        sims[i] = -1
        for j in np.argsort(-sims)[: args.neighbors]:
            if float(sims[j]) >= args.edge_threshold and int(j) not in cand:
                cand.add(int(j))
                added_by[int(j)] = (int(i), float(sims[j]))

    cand_list = sorted(cand, key=lambda i: -qsim[i])
    exp_idx = cand_list[: args.topk]

    def show(title, idxs):
        print(f"\n=== {title} ===")
        for r, i in enumerate(idxs, 1):
            m = meta[i]
            tag = ""
            if i in added_by:
                src, s = added_by[i]
                tag = f"  ← 由圖擴展帶入(鄰接 #{meta[src]['idx']}, 相似 {s:.3f})"
            print(f"{r}. [{qsim[i]:.3f}] {m['doc'][:34]} 第{m['page']}頁 #{m['idx']}{tag}")
            print(f"   {m['text'][:95].strip().replace(chr(10),' ')}")

    print(f"\n問題：{args.question}")
    show(f"對照組：一般檢索 top-{args.topk}", base_idx)
    show(f"實驗組：圖擴展後 top-{args.topk}", exp_idx)

    b, e = set(int(i) for i in base_idx), set(exp_idx)
    print("\n=== 差異 ===")
    print(f"  候選池：{len(base_idx)} → {len(cand)} 塊（圖擴展新增 {len(cand)-len(base_idx)}）")
    print(f"  最終 top-{args.topk} 相同：{len(b & e)} 塊　不同：{len(e - b)} 塊")
    if e - b:
        for i in e - b:
            m = meta[i]
            print(f"    + 新進榜：{m['doc'][:30]} 第{m['page']}頁 #{m['idx']}")
    else:
        print("    最終結果完全相同 → 圖擴展對這個問題沒有影響")


if __name__ == "__main__":
    main()
