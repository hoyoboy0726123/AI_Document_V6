# -*- coding: utf-8 -*-
"""精確計數題組：答案是「幾個」，地面真相取自知識圖譜的節點/邊數。

為什麼要單獨驗這一類：多跳題實測「有 KG / 無 KG」逐題同分（小文件裡兩張表
會一起進 context，LLM 自己就 join 完了）。KG 唯一無法被檢索取代的能力是
「數得準」—— 檢索只能給前 N 筆，數量本身不在任何一段文字裡。

評分刻意只認量詞形狀（共/總共 N 個），不用字串包含：答案表格裡到處都是
數字，包含比對會把「共計 16 個」因別處出現 30 而判對（實測踩過）。
"""
import json, pathlib, re, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import logging; logging.disable(logging.WARNING)
from app.main import apply_llm_overrides_from_db; apply_llm_overrides_from_db()
from app.database import SessionLocal
from app.services.agent import route_mode, run_agent, run_rag_only

_NUM = re.compile(r"(?:共計|總共|共有|共|合計|一共)\s*(?:有\s*)?(\d{1,4})\s*(?:個|項|條|份|種|類|章節|附錄|程序|方法)")
# 「有 8 個程序」這種沒有「共」字的也算
_NUM2 = re.compile(r"(\d{1,4})\s*(?:個|項|條|份|種|類)\s*(?:測試方法|子章節|測試程序|附錄|項目|外部規範|規範)")

def answer(db, q):
    if route_mode(q) == "agent":
        out = ""
        for ev in run_agent(db, q):
            if ev.get("type") == "final":
                out = ev.get("text") or ""
        return out
    return run_rag_only(db, q)[0]

def main():
    out_path = sys.argv[sys.argv.index("--out")+1] if "--out" in sys.argv else ""
    items = [json.loads(l) for l in (pathlib.Path(__file__).with_name("golden_count.jsonl")
             ).read_text(encoding="utf-8").splitlines() if l.strip()]
    db = SessionLocal(); rows=[]; ok=0; t0=time.perf_counter()
    for it in items:
        t=time.perf_counter()
        try: a = answer(db, it["q"])
        except Exception as e: a = f"(失敗: {e})"
        found = _NUM.findall(a or "") + _NUM2.findall(a or "")
        hit = it["gold"] in found
        ok += hit
        rows.append({"id":it["id"],"q":it["q"],"gold":it["gold"],"found":found[:5],
                     "hit":hit,"secs":round(time.perf_counter()-t,1),"len":len(a)})
        print(f"  {it['id']}  {'✅' if hit else '❌'}  正解 {it['gold']:>3}  "
              f"模型說 {found[:3] or '（沒說數量）'}  {rows[-1]['secs']:>5}s  {it['q'][:30]}", flush=True)
    db.close()
    res={"n":len(items),"score":round(ok/len(items),3),"seconds":round(time.perf_counter()-t0,1),"per_question":rows}
    print(f"\n  計數正確率 {res['score']}  ({ok}/{len(items)})   耗時 {res['seconds']}s")
    if out_path:
        pathlib.Path(out_path).write_text(json.dumps(res,ensure_ascii=False,indent=2),encoding="utf-8")
    return 0
sys.exit(main())
