"""
Tool registry exposed to the Agent. Each tool has:
  - name: stable identifier used in tool-call JSON
  - description: shown to the LLM in system prompt
  - schema: input parameter shape (Pydantic-like dict, not full Pydantic to keep prompt short)
  - run(db, params) -> dict: executes the tool, returns observation

Tools are intentionally narrow — they wrap KG service / RAG search / docs lookup.
The LLM never sees raw SQL.
"""
from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from .. import models
from ..core.config import settings
from . import ai, kg_service, retrieval

logger = logging.getLogger(__name__)


@dataclass
class Tool:
    name: str
    description: str
    schema: Dict[str, Any]  # JSON schema-ish; rendered into prompt
    run: Callable[[Session, Dict[str, Any]], Dict[str, Any]]


# ───── Tool implementations ────────────────────────────────────────────────


def _tool_rag_search(db: Session, params: Dict[str, Any]) -> Dict[str, Any]:
    query = str(params.get("query") or "").strip()
    if not query:
        return {"error": "query is required"}
    top_k = int(params.get("top_k") or 5)
    top_k = max(1, min(top_k, 10))

    embeddings = ai.embed_query(query)
    if not embeddings:
        return {"error": "embedding failed"}

    # 與 /rag/query 共用同一條檢索（services.retrieval，檢索邏輯的唯一實作）。
    # 檢索範圍取自本次請求（db.info）——原本這裡寫死「Agent 為全域搜尋」，
    # 於是使用者在介面上鎖定了某份文件，Agent 仍然整個資料庫亂查，
    # 答案引用完全不相干的規範。介面提供了選擇器就必須遵守它。
    from .agent import get_retrieval_scope  # 延遲載入避免循環匯入
    scope = get_retrieval_scope(db)
    selected = retrieval.hybrid_retrieve(db, query, embeddings[0], top_k, **scope)
    if not selected:
        return {"results": []}

    radius = getattr(settings, "RAG_NEIGHBOR_RADIUS", 1)
    expand_cap = getattr(settings, "RAG_EXPAND_MAX_CHARS", 2000)
    out: List[Dict[str, Any]] = []
    for chunk, score in selected:
        doc = chunk.document
        out.append({
            "document_id": doc.id,
            "title": doc.title,
            "page": chunk.page,
            "score": round(score, 4),
            # 小找大：命中塊完整保留 + 鄰塊填補（與 RAG /query 一致），snippet 供 ReAct 推理與合成引用。
            "snippet": retrieval.expand_chunk_text(db, chunk, radius=radius, max_chars=expand_cap),
        })
    return {"results": out}


def _tool_spec_lookup(db: Session, params: Dict[str, Any]) -> Dict[str, Any]:
    name = str(params.get("name") or "").strip()
    if not name:
        return {"error": "name is required"}
    rows = kg_service.search_entities(db, name, limit=10)
    return {
        "results": [
            {
                "id": r.id,
                "canonical_id": r.canonical_id,
                "type": r.type,
                "description": r.description,
            }
            for r in rows
        ]
    }


def _resolve_entity(db: Session, key: str) -> Optional[models.KGEntity]:
    if not key:
        return None
    row = db.query(models.KGEntity).filter_by(id=key).first()
    if row:
        return row
    return db.query(models.KGEntity).filter_by(canonical_id=key).first()


def _tool_spec_references(db: Session, params: Dict[str, Any]) -> Dict[str, Any]:
    key = str(params.get("spec_id") or params.get("entity_id") or params.get("canonical_id") or "").strip()
    ent = _resolve_entity(db, key)
    if not ent:
        return {"error": f"entity not found: {key}"}

    hops = int(params.get("hops") or 1)
    nodes, edges = kg_service.get_neighbors(db, ent.id, hops=hops)
    node_map = {n.id: n for n in nodes}

    out_refs: List[Dict[str, Any]] = []
    in_refs: List[Dict[str, Any]] = []
    for e in edges:
        if e.src_id == ent.id and e.dst_id != ent.id:
            other = node_map.get(e.dst_id)
            if other:
                out_refs.append({
                    "canonical_id": other.canonical_id,
                    "type": other.type,
                    "rel_type": e.rel_type,
                    "confidence": e.confidence,
                })
        elif e.dst_id == ent.id and e.src_id != ent.id:
            other = node_map.get(e.src_id)
            if other:
                in_refs.append({
                    "canonical_id": other.canonical_id,
                    "type": other.type,
                    "rel_type": e.rel_type,
                    "confidence": e.confidence,
                })
    return {
        "center": ent.canonical_id,
        "type": ent.type,
        "outgoing": out_refs,  # this spec references these
        "incoming": in_refs,   # these specs reference this one
    }


def _tool_spec_supersedes_chain(db: Session, params: Dict[str, Any]) -> Dict[str, Any]:
    key = str(params.get("spec_id") or params.get("entity_id") or params.get("canonical_id") or "").strip()
    ent = _resolve_entity(db, key)
    if not ent:
        return {"error": f"entity not found: {key}"}
    chain = kg_service.get_supersedes_chain(db, ent.id)
    return {
        "chain": [
            {"canonical_id": e.canonical_id, "type": e.type, "is_center": e.id == ent.id}
            for e in chain
        ]
    }


def _tool_document_get(db: Session, params: Dict[str, Any]) -> Dict[str, Any]:
    doc_id = str(params.get("doc_id") or params.get("document_id") or "").strip()
    if not doc_id:
        return {"error": "doc_id is required"}
    doc = db.query(models.Document).filter_by(id=doc_id).first()
    if not doc:
        return {"error": "document not found"}
    return {
        "id": doc.id,
        "title": doc.title,
        "ai_summary": (doc.ai_summary or "")[:1000],
        "classification": doc.classification.name if doc.classification else None,
        "metadata": doc.metadata_data or {},
        "page_count": (
            db.query(models.DocumentChunk).filter_by(document_id=doc.id).count()
        ),
    }


def _name_in_query(ename: str, ql: str) -> bool:
    """反向比對：實體名稱(或其去括號核心，如 'MIL-STD-810H (full, plain-text)' → 'MIL-STD-810H')
    是否出現在查詢字串內。讓「MIL-STD-810H 有哪些測試方法」也能對到該文件節點。"""
    nm = (ename or "").lower().strip()
    if len(nm) >= 3 and nm in ql:
        return True
    core = re.sub(r"\s*\(.*$", "", nm).strip()
    return len(core) >= 4 and core in ql


def _natural_key(number):
    """'12.10' 排在 '12.2' 之後（自然排序）。"""
    try:
        return [int(p) for p in str(number or "").split(".") if p != ""]
    except Exception:
        return [0]


def _tool_list_subitems(db: Session, params: Dict[str, Any]) -> Dict[str, Any]:
    """列出某文件/測試/章節的子項目（KG 結構：contains / part_of）。列舉題用這個，不要用 rag_search。"""
    name = str(params.get("name") or params.get("query") or "").strip()
    if not name:
        return {"error": "name is required"}
    rows = kg_service.search_entities(db, name, limit=10)
    if not rows:
        # 反向比對：LLM 常傳整句（如「ASUS NB 測試計畫中的 Pressure Test」）。
        # 找「實體名稱出現在查詢字串內」的結構節點，取最長(最具體)者。
        ql = name.lower()
        cand = db.query(models.KGEntity).filter(models.KGEntity.type.in_(["section", "document"])).all()
        rows = sorted(
            [e for e in cand if e.name and _name_in_query(e.name, ql)],
            key=lambda e: len(e.name or ""),
            reverse=True,
        )[:10]
    if not rows:
        return {"matched": None, "subitems": [], "hint": "KG 無對應節點；改用 rag_search"}

    q = name.lower()

    def _score(e) -> int:
        nm = (e.name or "").lower()
        s = 10 if e.type in ("section", "document") else 0
        if nm == q:
            s += 5
        elif q in nm or nm in q:
            s += 2
        return s

    ent = sorted(rows, key=_score, reverse=True)[0]

    # 子項目 = 出向 contains（文件→章節）+ 入向 part_of（子→母）
    child_ids: List[str] = [
        r.dst_id for r in db.query(models.KGRelation).filter_by(src_id=ent.id, rel_type="contains").all()
    ] + [
        r.src_id for r in db.query(models.KGRelation).filter_by(dst_id=ent.id, rel_type="part_of").all()
    ]
    subitems: List[Dict[str, Any]] = []
    if child_ids:
        emap = {e.id: e for e in db.query(models.KGEntity).filter(models.KGEntity.id.in_(child_ids)).all()}
        seen: set = set()
        child_ents: List[Any] = []
        for cid in child_ids:
            if cid in seen:
                continue
            seen.add(cid)
            c = emap.get(cid)
            if c:
                child_ents.append(c)
        # 文件層級「有哪些測試/方法」= 該標準的測試方法(method)，不是每個編號章節
        # (文件 contains 同時有 30 個 method + 24 個 section，混在一起且 section 編號小排在前會蓋掉方法)。
        if ent.type == "document":
            methods = [c for c in child_ents if c.type == "method"]
            if methods:
                child_ents = methods
        for c in child_ents:
            meta = c.meta or {}
            subitems.append({
                "name": c.name,
                "kind": meta.get("kind") or c.type,
                "number": meta.get("number"),
                "page": meta.get("page"),
                "document_id": meta.get("document_id"),
            })
        subitems.sort(key=lambda x: _natural_key(x.get("number")))

    # 該節點引用的標準
    refs: List[str] = []
    for r in db.query(models.KGRelation).filter_by(src_id=ent.id, rel_type="references").all():
        t = db.query(models.KGEntity).filter_by(id=r.dst_id).first()
        if t:
            refs.append(t.canonical_id)

    return {
        "matched": ent.name,
        "kind": (ent.meta or {}).get("kind") or ent.type,
        "subitems": subitems[:60],
        "subitem_count": len(subitems),
        "references": refs,
    }


def _has_children(db: Session, ent) -> bool:
    return bool(
        db.query(models.KGRelation).filter_by(src_id=ent.id, rel_type="contains").first()
        or db.query(models.KGRelation).filter_by(dst_id=ent.id, rel_type="part_of").first()
    )


def _resolve_named_structural(db: Session, name: str, prefer_parent: bool = False):
    """名稱 → 結構節點：先 forward 子字串搜尋，再 reverse-containment（名稱出現在查詢字串內）。

    prefer_parent=True：類別/覆蓋型查詢用 —— 同分時優先選「有子項目的父節點」，
    避免命中同名葉節點（如 'Pressure for Glass Test' 而非第 12 章 'Pressure Test'）。
    """
    rows = kg_service.search_entities(db, name, limit=10)
    if not rows:
        ql = name.lower()
        cand = db.query(models.KGEntity).filter(models.KGEntity.type.in_(["section", "document"])).all()
        rows = sorted(
            [e for e in cand if e.name and _name_in_query(e.name, ql)],
            key=lambda e: len(e.name or ""), reverse=True,
        )[:10]
    if not rows:
        return None
    q = name.lower()

    def _score(e) -> int:
        nm = (e.name or "").lower()
        s = 10 if e.type in ("section", "document") else 0
        if nm == q:
            s += 5
        elif q in nm or nm in q:
            s += 2
        if prefer_parent and _has_children(db, e):
            s += 4  # 類別查詢：有子項的父節點優先
        return s

    return sorted(rows, key=_score, reverse=True)[0]


_ASPECT_KW = {
    "criteria": ["criteria", "criterion", "判定", "驗收", "合格"],
    "specification": ["specification", "規格", "spec"],
    "objective": ["objective", "目的", "purpose"],
}


def _tool_get_subitem_details(db: Session, params: Dict[str, Any]) -> Dict[str, Any]:
    """逐項抓細節：對某父節點的每個子項目，分別抓出它自己的 criteria/spec/objective 段落。

    用於「這些測試的判定標準各是什麼」這類『細節橫跨多個子項目』的問題 —— top-k 檢索抓不齊，
    這裡用 KG 已知的每個子項目頁碼，逐一精準定位各自段落，保證全部涵蓋。
    """
    name = str(params.get("name") or params.get("query") or "").strip()
    aspect = str(params.get("aspect") or "criteria").strip().lower()
    if aspect not in _ASPECT_KW:
        aspect = "criteria"
    if not name:
        return {"error": "name is required"}
    ent = _resolve_named_structural(db, name)
    if not ent:
        return {"matched": None, "items": [], "hint": "KG 無對應節點"}

    child_ids = [
        r.dst_id for r in db.query(models.KGRelation).filter_by(src_id=ent.id, rel_type="contains").all()
    ] + [
        r.src_id for r in db.query(models.KGRelation).filter_by(dst_id=ent.id, rel_type="part_of").all()
    ]
    children = db.query(models.KGEntity).filter(models.KGEntity.id.in_(child_ids)).all() if child_ids else []
    # 葉節點（無子項目，如「Shock Test」）→ 取它自己的段落；有子項目 → 逐項取。
    is_leaf = not children
    targets = [ent] if is_leaf else children
    phrase = {"criteria": "Testing Criteria", "specification": "Testing Specification", "objective": "Testing Objective"}[aspect]
    kw = _ASPECT_KW[aspect]

    # 本文件所有 section 的頁碼（排序），用來算每個 target 的「下一節」邊界（章節常跨頁，用節邊界較準）。
    doc_id0 = (ent.meta or {}).get("document_id")
    all_pages = sorted({
        (e.meta or {}).get("page")
        for e in db.query(models.KGEntity).filter(models.KGEntity.type == "section").all()
        if (e.meta or {}).get("document_id") == doc_id0 and (e.meta or {}).get("page")
    })

    def _next_section_page(p):
        return next((x for x in all_pages if x > p), None)

    items: List[Dict[str, Any]] = []
    for c in sorted(targets, key=lambda e: ((e.meta or {}).get("page") or 0, _natural_key((e.meta or {}).get("number")))):
        meta = c.meta or {}
        doc_id, page = meta.get("document_id"), meta.get("page")
        detail = None
        if doc_id and page:
            next_page = _next_section_page(page)
            hi = (next_page - 1) if (next_page and next_page > page) else (page + 6)
            chunks = (
                db.query(models.DocumentChunk)
                .filter(
                    models.DocumentChunk.document_id == doc_id,
                    models.DocumentChunk.page >= page,
                    models.DocumentChunk.page <= hi,
                )
                .order_by(models.DocumentChunk.page, models.DocumentChunk.chunk_index)
                .all()
            )
            joined = "\n".join(ch.text or "" for ch in chunks)
            low = joined.lower()
            number = meta.get("number")
            ph = r"\s+".join(re.escape(w) for w in phrase.split())  # 對空白寬鬆，如 Testing\s+Criteria
            pos = -1
            # 1) 最精準：用「本項編號 + 子節 + 小節名」定位（如 "12.6.7 Testing Criteria"），
            #    避免抓到鄰項或下一個 section（最後一項範圍較寬時尤其重要）。
            if number:
                matches = list(re.finditer(re.escape(str(number)) + r"\.\d+\s*" + ph, joined, re.IGNORECASE))
                if matches:
                    m = matches[-1]  # 頁面內容偶有重複（前者常為誤抽），取最後一個實質段落
                    pm = re.search(ph, joined[m.start():], re.IGNORECASE)
                    pos = m.start() + (pm.start() if pm else 0)
            # 2) 退而求其次：第一次出現的小節名。
            if pos < 0:
                m = re.search(ph, joined, re.IGNORECASE)
                pos = m.start() if m else -1
            # 3) 最後用關鍵字。
            if pos < 0:
                for k in kw:
                    i = low.find(k.lower())
                    if i >= 0:
                        pos = i
                        break
            if pos >= 0:
                # 在「完整內文」中找下一個小節標題作為邊界（不是固定小視窗），
                # 這樣同一小節跨頁的後續內容（如 12.1 spec 的 Operation 部分）也會完整帶出。
                # 邊界只認「下一個子節標題」(Testing X)，不要把頁尾 Copyright 當邊界——
                # 因為頁尾會出現在小節中間的換頁處，誤當邊界會把後續(如 Operation 規格)整段切掉。
                tail = re.search(
                    r"(?:\d+(?:\.\d+){1,2}\s*)?"  # 一併切掉子節前的編號（如 12.1.8）
                    r"Testing\s+(?:Result|Objective|Specification|Procedure|Location|Criteria|Software|Equipment|Method)",
                    joined[pos + len(phrase):], re.IGNORECASE,
                )
                end = (pos + len(phrase) + tail.start()) if tail else (pos + 3500)
                seg = joined[pos:end]
                # 清掉頁尾/頁碼雜訊（把跨頁切斷的內容接回）。
                seg = re.sub(r"\s*Copyright\s*\d{4}[^\n]*", " ", seg, flags=re.IGNORECASE)
                seg = re.sub(r"\s*ASUS NB System Reliability[^\n]*", " ", seg, flags=re.IGNORECASE)
                seg = re.sub(r"\s*Page\s*\d+\b", " ", seg, flags=re.IGNORECASE)
                # 去除因抽取重複造成的相鄰重複行。
                seen_lines, deduped = set(), []
                for ln in seg.splitlines():
                    key = ln.strip()
                    if key and key in seen_lines:
                        continue
                    if key:
                        seen_lines.add(key)
                    deduped.append(ln)
                detail = "\n".join(deduped).strip()[:2400]
            elif joined:
                detail = joined[:600].strip()
        if detail:
            # 把黏在一起的條列項補換行（如「…allowed.3.The system…」）。
            # 只在「非數字字元 + 1~2 位數字. + 後接字母」時切，避免誤切小數（1.0/3.6/2.5）與章節號（12.4）。
            detail = re.sub(r"(?<=\S)(?<!\d)(\d{1,2}\.)(?=\s*[A-Za-z])", r"\n\1", detail)
            # 列號後緊接字母時補一個空格（2.During → 2. During）；小數因點後為數字而不受影響。
            detail = re.sub(r"(?<!\d)(\d{1,2}\.)(?=[A-Za-z])", r"\1 ", detail)
        items.append({
            "name": c.name, "number": meta.get("number"),
            "page": page, "document_id": doc_id, "detail": detail,
        })
    return {"matched": ent.name, "aspect": aspect, "items": items, "item_count": len(items), "is_leaf": is_leaf}


def _children_of(db: Session, ent) -> List[models.KGEntity]:
    """回傳某 KG 節點的子項目（contains 正向 + part_of 反向）。"""
    child_ids = [
        r.dst_id for r in db.query(models.KGRelation).filter_by(src_id=ent.id, rel_type="contains").all()
    ] + [
        r.src_id for r in db.query(models.KGRelation).filter_by(dst_id=ent.id, rel_type="part_of").all()
    ]
    return db.query(models.KGEntity).filter(models.KGEntity.id.in_(child_ids)).all() if child_ids else []


def _doc_section_pages(db: Session, doc_id: Optional[str]) -> List[int]:
    """文件內所有 section 的頁碼（排序），用來算每節的「下一節」邊界。"""
    return sorted({
        (e.meta or {}).get("page")
        for e in db.query(models.KGEntity).filter(models.KGEntity.type == "section").all()
        if (e.meta or {}).get("document_id") == doc_id and (e.meta or {}).get("page")
    })


def _section_text(db: Session, ent, all_pages: List[int], max_chars: int = 800) -> Optional[str]:
    """抓某節「整節」內容(頁界 = 本頁→下一節頁-1),清掉頁尾雜訊,截斷。與 aspect 無關。"""
    meta = ent.meta or {}
    doc_id, page = meta.get("document_id"), meta.get("page")
    if not (doc_id and page):
        return None
    next_page = next((x for x in all_pages if x > page), None)
    hi = (next_page - 1) if (next_page and next_page > page) else (page + 6)
    chunks = (
        db.query(models.DocumentChunk)
        .filter(
            models.DocumentChunk.document_id == doc_id,
            models.DocumentChunk.page >= page,
            models.DocumentChunk.page <= hi,
        )
        .order_by(models.DocumentChunk.page, models.DocumentChunk.chunk_index)
        .all()
    )
    joined = "\n".join(ch.text or "" for ch in chunks)
    # 對齊到「本節自己的標題」(頁面常以前一節的結尾表格開頭，避免摘錄抓到上一節尾巴)。
    num, nm = meta.get("number"), ent.name or ""
    start = 0
    if num:
        m = re.search(r"\b" + re.escape(str(num)) + r"\b", joined)
        if m:
            start = m.start()
    if start == 0 and nm:
        m = re.search(re.escape(nm), joined, re.IGNORECASE)
        if m:
            start = m.start()
    seg = joined[start:]
    seg = re.sub(r"\s*Copyright\s*\d{4}[^\n]*", " ", seg, flags=re.IGNORECASE)
    seg = re.sub(r"\s*ASUS NB System Reliability[^\n]*", " ", seg, flags=re.IGNORECASE)
    seg = re.sub(r"\s*Page\s*\d+\b", " ", seg, flags=re.IGNORECASE)
    return seg.strip()[:max_chars] or None


def _tool_coverage_check(db: Session, params: Dict[str, Any]) -> Dict[str, Any]:
    """完整性交叉檢查：回傳某主題在 KG 結構下的「全部子項目 + 每項內容摘錄」。

    用途:RAG/關鍵字常漏掉不含關鍵字的子項(如 12.4 Surface Deflection)。當問題是
    「介紹/說明某類測試」或追問「這些測試...」,呼叫此工具可保證涵蓋全部子項，
    不會因關鍵字漏撈而少答。葉節點(無子項)則回它自己的內容。
    """
    name = str(params.get("name") or params.get("query") or params.get("topic") or "").strip()
    if not name:
        return {"error": "name is required"}
    ent = _resolve_named_structural(db, name, prefer_parent=True)
    if not ent:
        return {"matched": None, "items": [], "hint": "KG 無對應節點"}

    children = _children_of(db, ent)
    is_leaf = not children
    targets = [ent] if is_leaf else children
    all_pages = _doc_section_pages(db, (ent.meta or {}).get("document_id"))

    items: List[Dict[str, Any]] = []
    for c in sorted(targets, key=lambda e: ((e.meta or {}).get("page") or 0, _natural_key((e.meta or {}).get("number")))):
        meta = c.meta or {}
        items.append({
            "name": c.name,
            "number": meta.get("number"),
            "page": meta.get("page"),
            "document_id": meta.get("document_id"),
            "excerpt": _section_text(db, c, all_pages, 800),
        })
    return {"matched": ent.name, "item_count": len(items), "is_leaf": is_leaf, "items": items}


_ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}


def _roman_to_int(s: str) -> int:
    s = s.upper()
    if s.isdigit():
        return int(s)
    total, prev = 0, 0
    for ch in reversed(s):
        v = _ROMAN.get(ch, 0)
        total += -v if v < prev else v
        prev = max(prev, v)
    return total or 999


# 標題形式（有破折號/冒號 + 大寫起頭的短標題）：PROCEDURE I - BLOWING DUST
_PROC_TITLE_RE = re.compile(
    r"\bPROCEDURE\s+([IVXLC]{1,5})\b\s*[-–—:]\s*([A-Z][A-Za-z0-9 ,/()\-]{1,45})",
)
# 僅偵測存在（含無標題者）：PROCEDURE I / Procedure II …（限羅馬數字，避免誤抓 '5. ANALYSIS'）
_PROC_ANY_RE = re.compile(r"\bPROCEDURE\s+([IVXLC]{1,5})\b", re.IGNORECASE)
_METHOD_NUM_RE = re.compile(r"\b(\d{3}\.\d{1,2})\b")


def _tool_list_procedures(db: Session, params: Dict[str, Any]) -> Dict[str, Any]:
    """列出某測試方法的「程序」(Procedure I/II/…)。程序不是 KG 節點，從該方法內文掃出。
    用於「Method X 有哪些程序 / what procedures does X have」。"""
    name = str(params.get("name") or params.get("method") or "").strip()
    if not name:
        return {"error": "name is required"}
    rows = kg_service.search_entities(db, name, limit=25)
    cands = [r for r in rows if r.type in ("method", "section", "annex")]
    if not cands:  # 從問句抽方法號（如 '516.8'）再試
        m = _METHOD_NUM_RE.search(name)
        if m:
            rows = kg_service.search_entities(db, m.group(1), limit=25)
            cands = [r for r in rows if r.type in ("method", "section", "annex")]
    if not cands:
        return {"matched": None, "procedures": [], "hint": "no method node; try rag_search"}
    cands.sort(key=lambda r: (r.type != "method", len(r.canonical_id or "")))
    node = cands[0]
    meta = node.meta or {}
    doc_id, page = meta.get("document_id"), meta.get("page")
    if not (doc_id and page):
        return {"matched": node.canonical_id, "name": node.name, "procedures": []}
    # 方法的 meta.page（標題偵測頁）可能比實際程序標題晚數頁，頁碼切片會錯位到下一方法。
    # 改用每頁的「METHOD XXX」頁首歸屬：掃一個寬窗，依「最近出現的 METHOD 號」判斷目前所在方法。
    qnum = node.canonical_id.split("#", 1)[1] if "#" in node.canonical_id else ""
    method_pages = sorted(p for p in (
        (e.meta or {}).get("page") for e in db.query(models.KGEntity).filter(models.KGEntity.type == "method").all()
        if (e.meta or {}).get("document_id") == doc_id) if p)
    next_page = next((x for x in method_pages if x > page), None)
    lo = max(1, page - 8)
    hi = (next_page + 3) if next_page else (page + 45)
    chunks = (
        db.query(models.DocumentChunk)
        .filter(models.DocumentChunk.document_id == doc_id,
                models.DocumentChunk.page >= lo, models.DocumentChunk.page <= hi)
        .order_by(models.DocumentChunk.page, models.DocumentChunk.chunk_index)
        .all()
    )
    joined = "\n".join(ch.text or "" for ch in chunks)
    events = []  # (pos, kind, num, title)
    for m in re.finditer(r"\bMETHOD\s+(\d{3}\.\d{1,2})\b", joined, re.IGNORECASE):
        events.append((m.start(), "M", m.group(1), None))
    for m in _PROC_ANY_RE.finditer(joined):
        if _roman_to_int(m.group(1)) <= 12:
            events.append((m.start(), "P", m.group(1).upper(), None))
    for m in _PROC_TITLE_RE.finditer(joined):
        if _roman_to_int(m.group(1)) <= 12:
            events.append((m.start(), "T", m.group(1).upper(), re.split(r"[.\n;]", m.group(2))[0].strip(" -–—:.")))
    events.sort(key=lambda e: e[0])
    titles: Dict[str, str] = {}
    present: set = set()
    cur = None
    for _pos, kind, num, title in events:
        if kind == "M":
            cur = num
        elif cur == qnum:
            present.add(num)
            if kind == "T" and (num not in titles or 0 < len(title) < len(titles.get(num) or "9" * 99)):
                titles[num] = title
    nums = sorted(present | set(titles.keys()), key=_roman_to_int)
    procs = [{"procedure": n, "title": titles.get(n, "")} for n in nums]
    return {"matched": node.canonical_id, "name": node.name, "procedures": procs, "count": len(procs)}


# ───── Registry ───────────────────────────────────────────────────────────


def _tool_section_lookup(db: Session, params: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve a METHOD / numbered section by name (e.g. 'Method 510.7', '510.7', '504.3 2.2.2')
    and return every external standard referenced anywhere in its sub-tree, with the citing section.
    Section canonical_ids are scope-keyed (doc:ID#510.7, doc:ID#510.7/4.1.1.4) so the whole
    sub-tree is one prefix LIKE."""
    name = str(params.get("name") or params.get("method") or params.get("section") or "").strip()
    if not name:
        return {"error": "name is required"}
    rows = kg_service.search_entities(db, name, limit=25)
    cands = [r for r in rows if r.type in ("method", "section", "annex")]
    if not cands:
        return {"error": f"no method/section node matches '{name}'", "hint": "try rag_search"}
    # prefer a method node, then the shortest canonical_id (closest to the named scope root)
    cands.sort(key=lambda r: (r.type != "method", len(r.canonical_id or "")))
    node = cands[0]
    prefix = node.canonical_id
    subtree = (
        db.query(models.KGEntity)
        .filter(
            (models.KGEntity.canonical_id == prefix)
            | (models.KGEntity.canonical_id.like(prefix + "/%"))
        )
        .all()
    )
    id_to_ent = {e.id: e for e in subtree}
    rel_rows = (
        db.query(models.KGRelation)
        .filter(models.KGRelation.src_id.in_(list(id_to_ent.keys())),
                models.KGRelation.rel_type == "references")
        .all()
    )
    dst_ids = list({r.dst_id for r in rel_rows})
    dst_ents = {e.id: e for e in db.query(models.KGEntity).filter(models.KGEntity.id.in_(dst_ids)).all()} if dst_ids else {}
    specs_total: set = set()
    by_section: List[Dict[str, Any]] = []
    for r in rel_rows:
        dst = dst_ents.get(r.dst_id)
        if not dst or dst.type in ("section", "method", "annex", "document"):
            continue
        specs_total.add(dst.canonical_id)
        src = id_to_ent.get(r.src_id)
        by_section.append({"section": (src.name if src else None), "references": dst.canonical_id})
    return {
        "matched": node.canonical_id,
        "name": node.name,
        "type": node.type,
        "subtree_nodes": len(subtree),
        "referenced_standards": sorted(specs_total),
        "by_section": by_section[:80],
    }


TOOLS: Dict[str, Tool] = {
    "rag_search": Tool(
        name="rag_search",
        description=(
            "Vector search across all ingested documents. Use for free-text "
            "queries about content (e.g. 'humidity test conditions')."
        ),
        schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "natural language search query"},
                "top_k": {"type": "integer", "description": "max results (1-10, default 5)"},
            },
            "required": ["query"],
        },
        run=_tool_rag_search,
    ),
    "spec_lookup": Tool(
        name="spec_lookup",
        description=(
            "Find specification entities by name or partial ID (e.g. 'ISO 9001', 'MIL-STD-810'). "
            "Returns the canonical_id you'll pass to spec_references / spec_supersedes_chain."
        ),
        schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        run=_tool_spec_lookup,
    ),
    "spec_references": Tool(
        name="spec_references",
        description=(
            "Given a spec canonical_id, return what it references (outgoing) and what "
            "references it (incoming). Use this to follow cross-references between specs."
        ),
        schema={
            "type": "object",
            "properties": {
                "spec_id": {"type": "string", "description": "canonical_id e.g. 'MIL-STD-810G'"},
                "hops": {"type": "integer", "description": "1-3 hops (default 1)"},
            },
            "required": ["spec_id"],
        },
        run=_tool_spec_references,
    ),
    "spec_supersedes_chain": Tool(
        name="spec_supersedes_chain",
        description=(
            "Trace the version chain of a spec — older versions it supersedes "
            "and newer versions that supersede it. Returns chain oldest→newest."
        ),
        schema={
            "type": "object",
            "properties": {"spec_id": {"type": "string"}},
            "required": ["spec_id"],
        },
        run=_tool_spec_supersedes_chain,
    ),
    "section_lookup": Tool(
        name="section_lookup",
        description=(
            "Given a METHOD or numbered section name/id (e.g. 'Method 510.7', '510.7', "
            "'504.3 2.2.2'), return EVERY external standard that method/section (and its whole "
            "sub-tree) references, with the citing section. USE THIS for questions like "
            "'what standards does Method 510.7 reference/cite', '方法 510.7 引用了哪些規範', "
            "'什麼標準被 §X 引用' — test methods/sections are graph nodes, so this is exact where "
            "rag_search would be vague or miss the reference list."
        ),
        schema={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "method/section name or number"}},
            "required": ["name"],
        },
        run=_tool_section_lookup,
    ),
    "list_procedures": Tool(
        name="list_procedures",
        description=(
            "List the test PROCEDURES (Procedure I, II, III …) of a test method. USE THIS for "
            "'what procedures does Method X have', 'Method X 有哪些程序/程序有哪些'. Procedures are "
            "NOT graph nodes — this scans the method's own text and returns each Procedure number "
            "with its title (e.g. 'Procedure I - Blowing Dust')."
        ),
        schema={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "method name/number, e.g. 'Method 516.8' or '516.8'"}},
            "required": ["name"],
        },
        run=_tool_list_procedures,
    ),
    "document_get": Tool(
        name="document_get",
        description="Fetch document metadata (title, summary, classification, page count) by ID.",
        schema={
            "type": "object",
            "properties": {"doc_id": {"type": "string"}},
            "required": ["doc_id"],
        },
        run=_tool_document_get,
    ),
    "list_subitems": Tool(
        name="list_subitems",
        description=(
            "List the COMPLETE set of sub-items / child sections of a document, test, or "
            "section BY NAME, from the knowledge-graph structure (contains / part_of). "
            "USE THIS — not rag_search — for enumeration questions: 'what tests/items does X "
            "have', '有哪些', 'list all ...', 'sub-tests of ...', 'X 的子項目'. "
            "Returns the exact full list (rag_search would miss items). Also returns the "
            "standards that node references."
        ),
        schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "document title / test / section name, e.g. 'Pressure Test'"},
            },
            "required": ["name"],
        },
        run=_tool_list_subitems,
    ),
    "get_subitem_details": Tool(
        name="get_subitem_details",
        description=(
            "For a parent test/document, return EACH sub-item's specific section content for an "
            "aspect (criteria / specification / objective). USE THIS for 'what are the criteria/"
            "specs/objectives of these tests', 'X 的判定標準/規格/目的 各是什麼', i.e. a DETAIL that "
            "spans many sub-items. Locates each sub-item's own section by its KG page, so all "
            "sub-items are covered (rag_search would miss most)."
        ),
        schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "parent name, e.g. 'Pressure Test'"},
                "aspect": {"type": "string", "description": "criteria | specification | objective (default criteria)"},
            },
            "required": ["name"],
        },
        run=_tool_get_subitem_details,
    ),
    "coverage_check": Tool(
        name="coverage_check",
        description=(
            "Completeness cross-check. For a topic/category that has sub-items (e.g. a chapter "
            "of tests), return the COMPLETE set of sub-items from the knowledge graph WITH a "
            "content excerpt for each. USE THIS whenever the user asks generally about a group/"
            "category ('介紹 X / 說明這些測試 / tell me about the X tests') or follows up about "
            "'these tests', AND after a rag_search whose results look partial — keyword/RAG misses "
            "sub-items that don't contain the keyword (e.g. a 'Surface Deflection' page under "
            "'Pressure Test'). Guarantees every sub-item is covered so the final answer is complete."
        ),
        schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "topic / category / parent name, e.g. 'Pressure Test'"},
            },
            "required": ["name"],
        },
        run=_tool_coverage_check,
    ),
}


def render_tools_for_prompt() -> str:
    """Render tool list for inclusion in the Agent system prompt."""
    lines: List[str] = []
    for tool in TOOLS.values():
        params = tool.schema.get("properties", {})
        param_descs = []
        for pname, pspec in params.items():
            ptype = pspec.get("type", "string")
            pdesc = pspec.get("description", "")
            required = pname in tool.schema.get("required", [])
            param_descs.append(f"    - {pname} ({ptype}{'*' if required else ''}): {pdesc}")
        params_str = "\n".join(param_descs) if param_descs else "    (no params)"
        lines.append(f"- {tool.name}: {tool.description}\n  params:\n{params_str}")
    return "\n".join(lines)


def _norm_tool(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _resolve_tool_name(name: str) -> Optional[str]:
    """容錯解析工具名：LLM 偶爾把工具名叫錯（rag_sarch、get_sumerules_item_details…）。
    依「正規化精確 → 同前綴動詞+片段重疊 → difflib 近似」找回最接近的有效工具名。"""
    if not name:
        return None
    keys = list(TOOLS.keys())
    if name in TOOLS:
        return name
    nlow = name.lower().strip()
    for k in keys:
        if k.lower() == nlow:
            return k
    nname = _norm_tool(name)
    for k in keys:  # 去掉底線/雜符後完全相同
        if _norm_tool(k) == nname:
            return k
    # token 重疊（同前綴動詞時更可信，如 get_*_details → get_subitem_details）
    ntoks = set(re.split(r"[^a-z0-9]+", nlow)) - {""}
    best, best_score = None, 0.0
    for k in keys:
        ktoks = set(re.split(r"[^a-z0-9]+", k.lower())) - {""}
        if not ktoks:
            continue
        inter = len(ntoks & ktoks)
        jacc = inter / len(ntoks | ktoks)
        score = jacc + (0.2 if (ntoks & ktoks and next(iter(k.split("_")), "") == nlow.split("_")[0]) else 0.0)
        if score > best_score:
            best, best_score = k, score
    if best and best_score >= 0.34:
        return best
    m = difflib.get_close_matches(nlow, [k.lower() for k in keys], n=1, cutoff=0.5)
    if m:
        for k in keys:
            if k.lower() == m[0]:
                return k
    return None


resolve_tool_name = _resolve_tool_name  # public alias（供 agent loop 在分派前正規化 action）


def run_tool(db: Session, name: str, params: Dict[str, Any]) -> Dict[str, Any]:
    tool = TOOLS.get(name)
    if not tool:
        resolved = _resolve_tool_name(name)
        if resolved:
            logger.info("tool name '%s' fuzzy-matched to '%s'", name, resolved)
            tool = TOOLS[resolved]
        else:
            return {"error": f"unknown tool: {name}. Valid: {list(TOOLS.keys())}"}
    try:
        return tool.run(db, params or {})
    except Exception as e:
        logger.warning("tool %s failed: %s", name, e, exc_info=True)
        return {"error": f"{name} failed: {e}"}
