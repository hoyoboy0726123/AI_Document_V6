"""表格關係抽取：把文件裡的 markdown 表格逐列變成知識圖譜的節點與邊。

為什麼需要這個（實測依據）：
  KG 現有的實體抽取是 9 組 regex，只認得規範編號（MIL-STD / ISO / ASTM…）。
  但語料裡真正的結構資訊多半在表格：全語料 11% 的塊含 markdown 表格，
  教育訓練文件更高達 70%。實測那份文件在 KG 裡只有 1 個節點（document 本身），
  「料件有哪些種類」「ER PR 是什麼」因此完全沒有結構化保證，只能靠向量排序碰運氣。

為什麼不是「AI 產生 regex」：
  regex 只能抓格式，抓不到語意。`MIL-STD-810H` 有固定形狀所以 regex 有效；
  「料件大類」是表格裡的一欄，沒有形狀可言。而且錯的 regex 會靜默污染整張圖。
  表格的欄位語意由人指定一次，抽取本身是確定性的 —— 同樣輸入永遠同樣輸出，
  不受模型能力影響，也能在寫入前完整預覽。

產出的圖形狀刻意沿用既有詞彙 `contains` / `part_of`，這樣 agent 的
`list_subitems` 工具不用改就能列舉（列舉題因此能走 KG 的完整清單，
而不是向量檢索的「前 N 名」）。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from .. import models
from . import kg_service

logger = logging.getLogger(__name__)

# markdown 表格列：以 | 起訖。分隔列（| --- | --- |）另外判斷。
_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")
_SEP_RE = re.compile(r"^[\s|:\-]+$")
# 一張表至少要有這麼多資料列才值得抽（避免把兩三列的排版表當成資料表）
_MIN_ROWS = 3


def _split_row(line: str) -> List[str]:
    m = _ROW_RE.match(line)
    if not m:
        return []
    return [c.strip() for c in m.group(1).split("|")]


def parse_tables(text: str) -> List[Dict[str, Any]]:
    """把一段文字裡的 markdown 表格切出來。回傳 [{headers, rows, n_cols}]。

    連續的 | 行視為同一張表；中間出現非表格行就切斷。分隔列（---）不算資料，
    但它的位置決定「前一列是不是表頭」。
    """
    tables: List[Dict[str, Any]] = []
    cur: List[List[str]] = []
    sep_at: Optional[int] = None

    def _flush():
        nonlocal cur, sep_at
        if len(cur) >= _MIN_ROWS:
            if sep_at == 1 and len(cur) >= 2:
                headers, rows = cur[0], cur[2:] if len(cur) > 2 else []
            else:
                headers, rows = [], [r for i, r in enumerate(cur) if i != sep_at]
            rows = [r for r in rows if any(c for c in r)]
            if rows:
                tables.append({
                    "headers": headers,
                    "rows": rows,
                    "n_cols": max(len(r) for r in rows),
                })
        cur, sep_at = [], None

    for line in (text or "").splitlines():
        cells = _split_row(line)
        if cells:
            if _SEP_RE.match(line.replace("|", " ")):
                sep_at = len(cur)
            cur.append(cells)
        else:
            _flush()
    _flush()
    return tables


def detect_tables(db: Session, document_id: str) -> List[Dict[str, Any]]:
    """列出一份文件裡所有可抽取的表格，供 UI 選擇。不寫入任何東西。"""
    chunks = (
        db.query(models.DocumentChunk)
        .filter(models.DocumentChunk.document_id == document_id)
        .order_by(models.DocumentChunk.chunk_index)
        .all()
    )
    out: List[Dict[str, Any]] = []
    for ch in chunks:
        for i, t in enumerate(parse_tables(ch.text or "")):
            out.append({
                "key": f"{ch.id}#{i}",
                "chunk_id": ch.id,
                "page": ch.page,
                "headers": t["headers"],
                "n_cols": t["n_cols"],
                "n_rows": len(t["rows"]),
                "sample_rows": t["rows"][:5],
            })
    return out


def _get_table(db: Session, table_key: str) -> Tuple[Optional[models.DocumentChunk], Optional[Dict[str, Any]]]:
    chunk_id, _, idx = table_key.partition("#")
    ch = db.query(models.DocumentChunk).filter_by(id=chunk_id).first()
    if not ch:
        return None, None
    tables = parse_tables(ch.text or "")
    try:
        return ch, tables[int(idx or 0)]
    except (ValueError, IndexError):
        return ch, None


# 明顯不是資料的儲存格（表頭殘留、空白佔位）
_JUNK = {"", "-", "—", "–", "n/a", "na", "null", "備註", "說明", "項目", "代號", "名稱"}


def build_plan(db: Session, document_id: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    """乾跑：算出「會建立什麼」，但不寫入。UI 先顯示這個給人確認。

    spec = {
        table_key:   detect_tables 回傳的 key
        parent_name: 母節點名稱（如「料件大類」）；子項目掛在它底下
        entity_type: 子節點的 KG 型別（預設 term）
        id_col:      當作 canonical id 的欄位序號
        label_col:   當作名稱的欄位序號
    }
    """
    ch, table = _get_table(db, spec.get("table_key") or "")
    if not table:
        return {"error": "找不到指定的表格（文件可能已重新向量化）"}

    id_col = int(spec.get("id_col", 0))
    label_col = int(spec.get("label_col", 1))
    parent_name = (spec.get("parent_name") or "").strip()
    if not parent_name:
        return {"error": "parent_name 不可為空"}
    etype = (spec.get("entity_type") or "term").strip() or "term"

    entities: List[Dict[str, str]] = []
    skipped: List[Dict[str, str]] = []
    seen: set = set()
    for row in table["rows"]:
        if id_col >= len(row) or label_col >= len(row):
            skipped.append({"row": " | ".join(row), "why": "欄位數不足"})
            continue
        code, label = row[id_col].strip(), row[label_col].strip()
        if code.lower() in _JUNK or label.lower() in _JUNK:
            skipped.append({"row": " | ".join(row), "why": "空值或表頭殘留"})
            continue
        cid = f"{parent_name}:{code}"
        if cid in seen:
            skipped.append({"row": " | ".join(row), "why": "重複代號"})
            continue
        seen.add(cid)
        entities.append({"canonical_id": cid, "code": code, "name": label})

    return {
        "parent": {"canonical_id": parent_name, "name": parent_name, "type": "category"},
        "entity_type": etype,
        "entities": entities,
        "skipped": skipped,
        "n_entities": len(entities),
        "n_relations": len(entities) * 2,   # contains + part_of
        "source": {"chunk_id": ch.id if ch else None, "page": ch.page if ch else None},
    }


def apply_plan(db: Session, document_id: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    """把 build_plan 的結果實際寫進 KG。呼叫端負責 commit。"""
    plan = build_plan(db, document_id, spec)
    if plan.get("error"):
        return plan

    p = plan["parent"]
    parent = kg_service.upsert_entity(
        db, canonical_id=p["canonical_id"], type_=p["type"], name=p["name"],
        meta={"document_id": document_id, "source": "table_extractor",
              "table_key": spec.get("table_key")},
    )
    n_ent = n_rel = 0
    for e in plan["entities"]:
        child = kg_service.upsert_entity(
            db, canonical_id=e["canonical_id"], type_=plan["entity_type"], name=e["name"],
            meta={"document_id": document_id, "source": "table_extractor",
                  "code": e["code"], "page": plan["source"]["page"]},
        )
        n_ent += 1
        for src, dst, rel in ((parent.id, child.id, "contains"), (child.id, parent.id, "part_of")):
            if kg_service.upsert_relation(
                db, src_id=src, dst_id=dst, rel_type=rel,
                document_id=document_id, chunk_id=plan["source"]["chunk_id"],
                confidence=1.0, evidence=f"表格抽取：{e['code']} = {e['name']}",
            ):
                n_rel += 1
    logger.info("表格抽取：文件 %s 建立 %d 節點 / %d 邊（母節點 %s）",
                document_id[:8], n_ent, n_rel, p["canonical_id"])
    return {"ok": True, "parent": p["canonical_id"], "n_entities": n_ent,
            "n_relations": n_rel, "skipped": len(plan["skipped"])}


def remove_plan(db: Session, parent_name: str) -> Dict[str, Any]:
    """撤銷一次抽取：刪掉母節點與它 contains 的所有子節點（含邊）。

    抽錯了要能一鍵回退 —— 沒有這個，使用者不敢按確認。
    """
    parent = db.query(models.KGEntity).filter_by(canonical_id=parent_name).first()
    if not parent:
        return {"ok": False, "error": "找不到該母節點"}
    child_ids = [r.dst_id for r in db.query(models.KGRelation)
                 .filter_by(src_id=parent.id, rel_type="contains").all()]
    ids = set(child_ids) | {parent.id}
    n_rel = (db.query(models.KGRelation)
             .filter(models.KGRelation.src_id.in_(ids) | models.KGRelation.dst_id.in_(ids))
             .delete(synchronize_session=False))
    n_ent = (db.query(models.KGEntity)
             .filter(models.KGEntity.id.in_(ids))
             .delete(synchronize_session=False))
    return {"ok": True, "n_entities": n_ent, "n_relations": n_rel}
