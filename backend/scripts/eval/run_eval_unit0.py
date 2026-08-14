"""[Unit 0]基礎課程 語料的 golden set 評測。

為什麼另開一套而不沿用 run_eval_types.py：那一套的題目、主體詞與評分規則
都是為 MIL-STD 英文規範語料寫的（ANNEX、METHOD、規範編號），對這份中文
教育訓練投影片不適用。

ground truth 取自 PDF 原件（第 7 頁編碼原則、第 11 頁類別代號表、原物料
介紹九宮格、NB 拆解頁），**不是**從系統目前的輸出反推 —— 否則等於拿現況
當標準答案，任何退化都量不出來。

題型與計分：
  coverage  關鍵詞命中率 = 命中數 / gold 總數
  exact     gold 全中且未出現 forbidden 才得 1 分，否則 0 分
  absent    語料中確實沒有的問題。誠實說查不到得 1 分；掰出具體內容得 0 分

用法：
    .venv\\Scripts\\python.exe scripts\\eval\\run_eval_unit0.py
    .venv\\Scripts\\python.exe scripts\\eval\\run_eval_unit0.py --baseline scripts\\eval\\unit0_baseline.json

注意：評測必須獨佔資源。與伺服器同時打 LLM 會量到不存在的回歸。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import httpx

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:8002/api/v1"
HERE = Path(__file__).resolve().parent

# 系統「誠實說查不到」時會出現的字樣。任一命中即視為正確拒答。
#
# 第一版漏掉了「沒有找到」（只寫了「找不到」），導致 u11 被誤判成編造 ——
# 實際上低信心閘門有正常觸發（日誌可見 run_rag_only 低信心兜底 CE=0.007），
# 系統回的是「⚠️ 資料庫中沒有找到與您問題『高度相關』的內容（檢索信心偏低）」。
# 評測的偽陰性比系統的真錯誤更危險：它會讓人跑去修一個不存在的問題。
# 因此這裡以「系統實際會輸出的字樣」為準，不是憑印象列舉。
_DECLINE_MARKERS = (
    "查無", "找不到", "沒有找到", "查詢不到", "沒有查到",
    "信心偏低", "不直接相關", "可能不相關",
    "沒有提供", "未提供", "無法回答", "不足",
    "無相關", "沒有相關", "未提及", "沒有提及", "無法從", "並未",
)


def _norm(s: str) -> str:
    return "".join(s.split()).upper()


def _hits(golds: list[str], text: str) -> int:
    t = _norm(text)
    return sum(1 for g in golds if _norm(g) in t)


def _declined(text: str) -> bool:
    return any(m in text for m in _DECLINE_MARKERS)


def ask(client: httpx.Client, headers: dict, question: str) -> tuple:
    """走混合模式（前端預設）。空歷史，避免前一題的主體污染改寫。

    回傳 (answer, sources)：citation 題型要用 sources 驗證 [來源N] 指向誰。
    """
    event, answer, sources = None, "", []
    with client.stream("POST", f"{BASE}/agent/route", headers=headers, json={
        "question": question, "top_k": 5, "max_steps": 8,
        "conversation_history": [],
    }) as r:
        for line in r.iter_lines():
            if line.startswith("event: "):
                event = line[7:].strip()
                continue
            if not line.startswith("data: "):
                continue
            data = json.loads(line[6:])
            if event == "final":
                answer = data.get("text") or ""
                sources = data.get("sources") or []
            elif event == "error":
                answer = f"[ERROR] {data.get('message')}"
    return answer, sources


def score_item(item: dict, answer: str, sources: list) -> tuple[float, str]:
    kind = item["kind"]
    if kind == "absent":
        return (1.0, "正確拒答") if _declined(answer) else (0.0, "未拒答（可能編造）")

    golds = item.get("gold") or []
    hit = _hits(golds, answer)

    if kind == "citation":
        # 引用正確率：答案裡每個 [來源N]，其 sources[N-1] 的 snippet 必須含
        # cite_anchor（該題內容獨有的鑑別詞）。全部命中 = 1，按比例給分。
        # 內容先要及格（gold 過半），否則沒引用可驗也沒意義。
        if hit * 2 < len(golds):
            return 0.0, f"內容不及格 {hit}/{len(golds)}"
        import re as _re
        cited = _re.findall(r"\[來源(\d+)\]", answer)
        if not cited:
            return 0.0, "沒有任何引用標記"
        anchor = item.get("cite_anchor") or ""
        ok = sum(1 for n in cited
                 if 0 < int(n) <= len(sources)
                 and anchor in (sources[int(n) - 1].get("snippet") or ""))
        return ok / len(cited), f"引用正確 {ok}/{len(cited)}"

    if kind == "exact":
        bad = [f for f in (item.get("forbidden") or []) if _norm(f) in _norm(answer)]
        if bad:
            return 0.0, f"命中 forbidden：{bad}"
        return (1.0, "正確") if hit == len(golds) else (0.0, f"缺 {len(golds) - hit} 項")
    return hit / len(golds) if golds else 0.0, f"{hit}/{len(golds)}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", help="與先前結果比較的 json 路徑")
    ap.add_argument("--out", help="輸出 json 路徑")
    ap.add_argument("--label", default="unit0")
    args = ap.parse_args()

    items = [json.loads(l) for l in
             (HERE / "golden_unit0.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]

    with httpx.Client(timeout=1800.0) as c:
        tok = c.post(f"{BASE}/auth/login",
                     data={"username": "admin", "password": "Admin@123"}).json()["access_token"]
        h = {"Authorization": f"Bearer {tok}"}

        results, t_start = [], time.perf_counter()
        for it in items:
            t0 = time.perf_counter()
            ans, srcs = ask(c, h, it["q"])
            dt = time.perf_counter() - t0
            sc, detail = score_item(it, ans, srcs)
            results.append({"id": it["id"], "kind": it["kind"], "q": it["q"],
                            "score": round(sc, 3), "detail": detail,
                            "answer_len": len(ans), "seconds": round(dt, 1)})
            print(f"  {it['id']}  {it['kind']:<9} {sc:>5.2f}  {detail:<22} "
                  f"{len(ans):>5}字 {dt:>5.1f}s  {it['q'][:24]}")

    by_kind: dict[str, list[float]] = {}
    for r in results:
        by_kind.setdefault(r["kind"], []).append(r["score"])

    summary = {
        "label": args.label,
        "n": len(results),
        "overall": round(statistics.mean(r["score"] for r in results), 3),
        "by_kind": {k: round(statistics.mean(v), 3) for k, v in by_kind.items()},
        "total_seconds": round(time.perf_counter() - t_start, 1),
        "per_question": results,
    }

    print("\n" + "=" * 62)
    print(f"  總分 {summary['overall']:.3f}   耗時 {summary['total_seconds']}s")
    for k, v in summary["by_kind"].items():
        print(f"    {k:<10} {v:.3f}")

    if args.baseline:
        try:
            base = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"  （讀不到基線：{exc}）")
        else:
            delta = summary["overall"] - base["overall"]
            sign = "+" if delta >= 0 else ""
            print(f"\n  對照基線 {base.get('label')}：{base['overall']:.3f} "
                  f"→ {summary['overall']:.3f}  ({sign}{delta:.3f})")
            prev = {p["id"]: p["score"] for p in base["per_question"]}
            for r in results:
                d = r["score"] - prev.get(r["id"], 0.0)
                if abs(d) >= 0.05:
                    print(f"    {r['id']}  {prev.get(r['id'], 0):.2f} → {r['score']:.2f}  "
                          f"({'+' if d > 0 else ''}{d:.2f})  {r['q'][:28]}")

    if args.out:
        Path(args.out).write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        print(f"\n  已寫出 {args.out}")


if __name__ == "__main__":
    main()
