"""表格自動抽取：LLM 當設定產生器，規則引擎當執行者，完整性由建構保證。

Graphify 式「LLM 自由抽取三元組」實測輸給確定性表格抽取（multihop 0.75 vs 1.0）：
雙欄對照表漏掉右半邊（30 個代號只抓到 20）、沒形成統一母節點、雜訊三元組
兩度干擾原本答對的題。根因不是模型不夠大，而是一次性抽取抽完不回頭看，
且自由文字沒有「應該抽出幾條」的基準可驗。

這裡把分工反過來 —— LLM 只做它擅長且錯了看得到的小決策：
    「這張表值不值得抽？母節點叫什麼？哪欄是代號、哪欄是名稱？」
逐列建節點交給 kg_tables 的確定性引擎（30 列就是 30 個節點，不受模型能力影響），
再用「節點數 + 略過數 == 表格列數」的恆等式當驗證環。LLM 的錯誤只可能出現在
欄位對映，會直接反映在乾跑預覽裡，不會靜默污染圖譜。

模型：預設走本地 Ollama（表格內容不出門）。建議用大一點的本地模型
（如 qwen3.8:27b）；呼叫帶 keep_alive=0，用完即卸載，不佔住 VRAM。
"""
from __future__ import annotations

import json
import logging
import re
import urllib.request
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from .. import models
from ..core.config import settings
from . import kg_service, kg_tables

logger = logging.getLogger(__name__)

_PROMPT = """你是知識圖譜的表格分析器。判斷下面這張表格是否值得抽取成「母節點 → 子項目」結構。

值得抽的表：代號/名稱對照表、分類清單、術語定義表 —— 每一列是一個獨立項目。
不值得抽的表：版面排版用的表、敘述性長句的表、鍵值設定表、每列不是同一種東西的表。

規則：
- parent_name：這批子項目的集合名稱，用文件內文的稱呼（例如出現「類別代號」的表，母節點多半叫「料件大類」或表格上方標題的名詞）。精簡名詞，不要句子。
- col_pairs：哪一欄是代號(id_col)、哪一欄是名稱(label_col)，欄位序號從 0 起算。
  「代號|名稱|代號|名稱」這種左右兩組的對照表要回傳兩組 col_pairs。
- 沒有代號欄的清單表：id_col 與 label_col 可以指同一欄。
- 不確定就 worth=false，寧可漏抽不要亂抽。

只輸出 JSON，不要解釋：
{"worth": true/false, "reason": "一句話", "parent_name": "...", "entity_type": "term",
 "col_pairs": [{"id_col": 0, "label_col": 1}]}

表格（頁 {page}，共 {n_rows} 列，表頭 {headers}）：
{rows}
"""

# 可自動套用的門檻：每組欄位至少抽出這麼多節點、且略過比例不超過這個值。
# 低於門檻不代表錯，代表「LLM 的欄位對映可能不對」→ 降級為需人工確認。
_AUTO_MIN_ENTITIES = 3
_AUTO_MAX_SKIP_RATIO = 0.34


def _chat_ollama(prompt: str, model: str) -> str:
    """直接呼叫 Ollama HTTP API 而不走 ollama_client：需要 keep_alive=0
    （建議模型可能是 17GB 的大傢伙，用完必須讓出 VRAM，而 client 的
    keep_alive 是全域設定，改它會影響翻譯/嵌入模型的常駐策略）。"""
    payload = {
        "model": model, "stream": False, "format": "json", "keep_alive": 0,
        "think": False,
        "options": {"temperature": 0.0, "num_ctx": 4096},
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        f"{settings.OLLAMA_BASE_URL}/api/chat",
        json.dumps(payload).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read())["message"]["content"]


def _rows_text(t: Dict[str, Any], limit: int = 8) -> str:
    lines = [" | ".join(r) for r in t.get("sample_rows", [])[:limit]]
    return "\n".join(lines)


def _suggest_one(t: Dict[str, Any], model: str) -> Dict[str, Any]:
    prompt = (_PROMPT
              .replace("{page}", str(t.get("page")))
              .replace("{n_rows}", str(t.get("n_rows")))
              .replace("{headers}", json.dumps(t.get("headers") or [], ensure_ascii=False))
              .replace("{rows}", _rows_text(t)))
    raw = _chat_ollama(prompt, model)
    d = json.loads(raw)
    pairs = []
    for p in d.get("col_pairs") or []:
        try:
            idc, lbc = int(p["id_col"]), int(p["label_col"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= idc < t["n_cols"] and 0 <= lbc < t["n_cols"]:
            pairs.append({"id_col": idc, "label_col": lbc})
    return {
        "worth": bool(d.get("worth")) and bool(pairs),
        "reason": str(d.get("reason") or "")[:200],
        "parent_name": re.sub(r"\s+", "", str(d.get("parent_name") or ""))[:60],
        "entity_type": (str(d.get("entity_type") or "term").strip() or "term")[:30],
        "col_pairs": pairs,
    }


def _verify_pairs(db: Session, document_id: str, table_key: str,
                  sug: Dict[str, Any]) -> Dict[str, Any]:
    """驗證環（確定性）：對每組欄位乾跑 build_plan，檢查抽出數量是否合理。

    build_plan 保證「每一列不是節點就是被記錄理由的略過」，所以完整性恆等式
    （entities + skipped == rows）由建構成立；這裡驗的是 LLM 那個小決策 ——
    欄位指錯時 skipped 比例會暴增或節點數過低，據此把該表降級為人工確認。
    """
    plans, total_entities, total_skipped = [], 0, 0
    for p in sug["col_pairs"]:
        plan = kg_tables.build_plan(db, document_id, {
            "table_key": table_key, "parent_name": sug["parent_name"],
            "entity_type": sug["entity_type"], **p})
        if plan.get("error"):
            return {"verdict": "error", "why": plan["error"], "plans": []}
        plans.append({**p, "n_entities": plan["n_entities"],
                      "n_skipped": len(plan["skipped"]),
                      "entities_preview": plan["entities"][:5],
                      "codes": [e["code"] for e in plan["entities"]]})
        total_entities += plan["n_entities"]
        total_skipped += len(plan["skipped"])
    n_rows_total = total_entities + total_skipped
    ok = (total_entities >= _AUTO_MIN_ENTITIES
          and all(pl["n_entities"] >= _AUTO_MIN_ENTITIES for pl in plans)
          and (total_skipped / n_rows_total if n_rows_total else 1) <= _AUTO_MAX_SKIP_RATIO)
    return {"verdict": "auto" if ok else "review",
            "why": ("" if ok else
                    f"抽出 {total_entities} 節點 / 略過 {total_skipped} 列，低於自動套用門檻"),
            "n_entities": total_entities, "n_skipped": total_skipped, "plans": plans}


def suggest(db: Session, document_id: str, model: Optional[str] = None) -> Dict[str, Any]:
    """對文件所有表格產生抽取建議 + 驗證結果。只乾跑，不寫入。"""
    model = (model or "").strip() or settings.OLLAMA_LLM_MODEL
    tables = kg_tables.detect_tables(db, document_id)
    out: List[Dict[str, Any]] = []
    for t in tables:
        try:
            sug = _suggest_one(t, model)
        except Exception as e:  # noqa: BLE001 — 單表失敗不拖垮整批
            logger.warning("表格建議失敗 %s: %s", t["key"], e)
            out.append({"table": t, "worth": False, "verdict": "error",
                        "why": f"LLM 建議失敗: {e}"})
            continue
        if not sug["worth"] or not sug["parent_name"]:
            out.append({"table": t, **sug, "verdict": "skip"})
            continue
        ver = _verify_pairs(db, document_id, t["key"], sug)
        out.append({"table": t, **sug, **ver})
    groups = _merge_groups(out, model)
    return {"model": model, "n_tables": len(tables), "suggestions": out,
            "groups": groups}



_UNIFY_PROMPT = """以下幾張表格經比對，抽出的代號高度重疊，實際上是「同一批項目」被印在文件的不同頁
（或被切頁截斷）。請為這批項目選一個「使用者最可能拿來提問」的母節點主名稱，其餘名稱列為別名。

候選名稱（來自各表的建議）：{names}
項目樣本：{samples}

只輸出 JSON：{"name": "主名稱", "aliases": ["別名1", "別名2"]}
"""


def _merge_groups(suggestions: List[Dict[str, Any]], model: str) -> List[Dict[str, Any]]:
    """同一批實體常被 OCR 切成多張表（續頁碎片、重印頁），逐表獨立決策會建出
    分裂的母節點 —— 實測 30 個代號被切成「類別代號30 / 料件大類14 / 領別16」
    三個母節點，問總數會答 14，錯得比沒有圖譜更糟。

    分組是確定性的：兩張表抽出的代號集合，交集佔小集合五成以上就視為同一批。
    LLM 只再做一個小決策：這批東西的主名稱叫什麼（其餘存為別名，路由都認得）。
    """
    cands = [s for s in suggestions
             if s.get("worth") and s.get("verdict") in ("auto", "review") and s.get("plans")]
    codes = [set(c for pl in s["plans"] for c in pl.get("codes", [])) for s in cands]
    parent = list(range(len(cands)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(cands)):
        for j in range(i + 1, len(cands)):
            small = min(len(codes[i]), len(codes[j]))
            if small and len(codes[i] & codes[j]) / small >= 0.5:
                parent[find(i)] = find(j)

    groups: Dict[int, List[int]] = {}
    for i in range(len(cands)):
        groups.setdefault(find(i), []).append(i)

    out: List[Dict[str, Any]] = []
    for members in groups.values():
        subs = [cands[i] for i in members]
        names = sorted({x["parent_name"] for x in subs})
        # 主名稱預設：抽出數最多的那張表的命名（資訊最完整的表通常命名也最準）
        primary = max(subs, key=lambda x: x.get("n_entities", 0))["parent_name"]
        aliases = [n for n in names if n != primary]
        if len(names) > 1:
            sample = sorted(set().union(*(codes[i] for i in members)))[:8]
            try:
                d = json.loads(_chat_ollama(
                    _UNIFY_PROMPT.replace("{names}", json.dumps(names, ensure_ascii=False))
                                 .replace("{samples}", json.dumps(sample, ensure_ascii=False)),
                    model))
                nm = re.sub(r"\s+", "", str(d.get("name") or ""))[:60]
                if nm:
                    primary = nm
                    aliases = sorted({a for a in names + [str(x)[:60] for x in (d.get("aliases") or [])]
                                      if a and a != primary})
            except Exception as e:  # noqa: BLE001 — 統一命名失敗就用預設主名稱
                logger.warning("母節點統一命名失敗（沿用預設 %s）: %s", primary, e)
        out.append({
            "primary_name": primary,
            "aliases": aliases,
            "member_keys": [x["table"]["key"] for x in subs],
            "n_codes_union": len(set().union(*(codes[i] for i in members))),
            "verdict": "auto" if any(x.get("verdict") == "auto" for x in subs) else "review",
            "entity_type": subs[0].get("entity_type") or "term",
        })
    return out


def auto_apply(db: Session, document_id: str, model: Optional[str] = None,
               keys: Optional[List[str]] = None) -> Dict[str, Any]:
    """跑 suggest 後以「群組」為單位套用：verdict=auto 的群組自動寫入；keys 內
    有任一成員表的群組（使用者看過預覽放行）即使 review 也寫入。呼叫端 commit。

    同群組所有表都掛在 primary_name 底下（碎片表補全主表），別名寫進母節點
    meta.aliases —— 路由與列舉工具靠它認得「料件大類/類別代號/領別」是同一個。
    寫入後回報母節點實際 contains 邊數作最終驗證。
    """
    res = suggest(db, document_id, model)
    sug_by_key = {x["table"]["key"]: x for x in res["suggestions"] if x.get("plans")}
    applied, skipped = [], []
    for g in res["groups"]:
        should = g["verdict"] == "auto" or (keys and any(k in keys for k in g["member_keys"]))
        if not should:
            skipped.append({"primary_name": g["primary_name"], "verdict": g["verdict"],
                            "member_keys": g["member_keys"]})
            continue
        n_ent = n_rel = 0
        for key in g["member_keys"]:
            sx = sug_by_key.get(key)
            if not sx:
                continue
            for pair in sx["col_pairs"]:
                r = kg_tables.apply_plan(db, document_id, {
                    "table_key": key, "parent_name": g["primary_name"],
                    "entity_type": g["entity_type"],
                    "id_col": pair["id_col"], "label_col": pair["label_col"]})
                if r.get("error"):
                    logger.warning("auto_apply 套用失敗 %s: %s", key, r["error"])
                    continue
                n_ent += r["n_entities"]; n_rel += r["n_relations"]
        parent = db.query(models.KGEntity).filter_by(canonical_id=g["primary_name"]).first()
        n_contains = 0
        if parent:
            if g["aliases"]:
                meta = dict(parent.meta or {})
                meta["aliases"] = sorted(set(meta.get("aliases") or []) | set(g["aliases"]))
                parent.meta = meta
            n_contains = (db.query(models.KGRelation)
                          .filter(models.KGRelation.src_id == parent.id,
                                  models.KGRelation.rel_type == "contains").count())
        applied.append({"primary_name": g["primary_name"], "aliases": g["aliases"],
                        "member_keys": g["member_keys"], "n_new_entities": n_ent,
                        "n_new_relations": n_rel, "n_contains_total": n_contains})
    return {"model": res["model"], "applied": applied, "skipped": skipped}
