"""相似度關聯圖 PoC —— 只讀分析，不寫入任何資料。

用資料庫裡「已經算好」的 embedding 建語意關聯圖，完全不呼叫 LLM。
對照組是 LightRAG：後者每份文件要跑約 1 小時 LLM 抽取，這裡是秒級。

用法：
    .venv\\Scripts\\python.exe scripts/similarity_graph_poc.py --title-like "%Unit 0%"
    .venv\\Scripts\\python.exe scripts/similarity_graph_poc.py --title-like "%810H%" --topk 5
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time

import numpy as np


def load_chunks(con: sqlite3.Connection, title_like: str):
    rows = con.execute(
        """select ch.id, ch.chunk_index, ch.page, ch.text, ch.embedding, d.title
           from document_chunks ch join documents d on d.id = ch.document_id
           where d.title like ? and ch.embedding is not null
           order by ch.chunk_index""",
        (title_like,),
    ).fetchall()
    if not rows:
        raise SystemExit(f"找不到符合 {title_like} 且有向量的 chunk")
    vecs = np.array([json.loads(r[4]) for r in rows], dtype="float32")
    meta = [
        {"id": r[0], "idx": r[1], "page": r[2], "text": r[3], "doc": r[5]} for r in rows
    ]
    return meta, vecs


def cosine_knn(vecs: np.ndarray, topk: int):
    """回傳每個節點的 topk 相鄰（含自己，稍後排除）。用內積＝餘弦（先做 L2 正規化）。"""
    norm = vecs / (np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-12)
    sim = norm @ norm.T  # 小規模直接算完整矩陣即可
    np.fill_diagonal(sim, -1.0)
    k = min(topk, sim.shape[0] - 1)
    idx = np.argpartition(-sim, kth=k - 1, axis=1)[:, :k]
    return sim, idx


def build_graph(meta, sim, idx, threshold: float, min_gap: int):
    """建邊。排除「同文件且 chunk_index 相鄰」的邊——那是切塊重疊造成的假關聯。"""
    edges, skipped_adjacent = [], 0
    n = len(meta)
    for i in range(n):
        for j in idx[i]:
            j = int(j)
            if j <= i:
                continue
            s = float(sim[i, j])
            if s < threshold:
                continue
            if abs(meta[i]["idx"] - meta[j]["idx"]) <= min_gap:
                skipped_adjacent += 1
                continue
            edges.append((i, j, s))
    return edges, skipped_adjacent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="doc_management.db")
    ap.add_argument("--title-like", default="%Unit 0%")
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--threshold", type=float, default=0.55)
    ap.add_argument("--min-gap", type=int, default=1, help="排除 chunk_index 差距 <= 此值的邊")
    ap.add_argument("--samples", type=int, default=6)
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    t0 = time.perf_counter()
    meta, vecs = load_chunks(con, args.title_like)
    load_t = time.perf_counter() - t0

    t1 = time.perf_counter()
    sim, idx = cosine_knn(vecs, args.topk)
    edges, skipped = build_graph(meta, sim, idx, args.threshold, args.min_gap)
    calc_t = time.perf_counter() - t1

    print(f"文件：{meta[0]['doc']}")
    print(f"  chunk 數：{len(meta)}　維度：{vecs.shape[1]}")
    print(f"  讀取 {load_t:.2f}s　計算 {calc_t:.2f}s　(無任何 LLM 呼叫)")
    print(f"  相似度門檻 {args.threshold}　topk {args.topk}")
    print(f"  產生邊：{len(edges)}　排除相鄰重疊邊：{skipped}")

    try:
        import networkx as nx

        g = nx.Graph()
        g.add_nodes_from(range(len(meta)))
        g.add_weighted_edges_from(edges)
        comps = list(nx.connected_components(g))
        isolated = sum(1 for c in comps if len(c) == 1)
        print(f"  連通群：{len(comps)}（其中孤立節點 {isolated}）")
        if g.number_of_edges():
            communities = list(nx.community.greedy_modularity_communities(g))
            print(f"  主題分群：{len(communities)} 群，大小 {[len(c) for c in communities]}")
            for ci, comm in enumerate(communities[:4], 1):
                pages = sorted({meta[i]["page"] for i in comm if meta[i]["page"]})
                print(f"    群{ci}：{len(comm)} 塊，頁碼 {pages[:12]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  (networkx 分析略過：{exc})")

    print("\n=== 相關段落樣本（判斷品質用）===")
    for i, j, s in sorted(edges, key=lambda e: -e[2])[: args.samples]:
        a, b = meta[i], meta[j]
        print(f"\n相似度 {s:.3f}　第{a['page']}頁(#{a['idx']}) ←→ 第{b['page']}頁(#{b['idx']})")
        print(f"  A: {a['text'][:110].strip().replace(chr(10),' ')}")
        print(f"  B: {b['text'][:110].strip().replace(chr(10),' ')}")

    con.close()


if __name__ == "__main__":
    main()
