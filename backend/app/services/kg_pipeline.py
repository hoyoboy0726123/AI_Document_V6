"""
KG extraction pipeline — runs after a document is ingested and chunked.

Per chunk:
  1. Regex extract all spec IDs (kg_extractor.extract_specs)
  2. Upsert each unique spec as a KGEntity
  3. For each pair of specs in the same chunk, ask LLM (cheap, max ~6 pairs/chunk)
     to classify the relation. Only references/supersedes/defines/requires/derives_from
     enter the graph; "none" is dropped.
  4. The document's own spec ID (if its title contains one) becomes the implicit
     source of all "references" found in its body.

Designed to be safe to re-run — uses upsert_entity / upsert_relation so a partial
failure can resume cleanly.
"""
from __future__ import annotations

import logging
from itertools import combinations
from typing import List, Optional

from sqlalchemy.orm import Session

from .. import models
from ..core.config import settings
from . import kg_extractor, kg_service
from .kg_extractor import SpecMatch
from .kg_relations import classify_pair
from .llm_provider import get_llm_provider

logger = logging.getLogger(__name__)


# Max pairs to send to LLM per chunk — keeps cost bounded on densely-cited
# chunks (e.g. a reference list page with 30 spec IDs would otherwise generate
# C(30,2) = 435 pairs).
_MAX_PAIRS_PER_CHUNK = 8

# When more than _MAX_PAIRS_PER_CHUNK pairs would be generated, prefer pairs
# where the source-side spec is the document's own ID (when discoverable).
# Otherwise we deduplicate-by-first-seen.


def _find_doc_spec(title: str) -> Optional[SpecMatch]:
    """If the document title contains a spec ID, treat it as the doc's identity."""
    if not title:
        return None
    matches = kg_extractor.extract_specs(title)
    return matches[0] if matches else None


def _pick_pairs(chunk_specs: List[SpecMatch], doc_spec_id: Optional[str]) -> List[tuple]:
    """Choose which (src, dst) pairs to send to LLM. Returns canonical_id pairs."""
    ids = [s.canonical_id for s in chunk_specs]
    if doc_spec_id and doc_spec_id not in ids:
        # doc spec not in this chunk's specs — pair doc_spec with each found spec
        return [(doc_spec_id, sid) for sid in ids][:_MAX_PAIRS_PER_CHUNK]

    if doc_spec_id:
        # doc spec is mentioned in chunk — pair it with the others first
        others = [sid for sid in ids if sid != doc_spec_id]
        pairs = [(doc_spec_id, sid) for sid in others]
        if len(pairs) < _MAX_PAIRS_PER_CHUNK and len(others) > 1:
            # fill remaining budget with cross-pairs
            for a, b in combinations(others, 2):
                pairs.append((a, b))
                if len(pairs) >= _MAX_PAIRS_PER_CHUNK:
                    break
        return pairs[:_MAX_PAIRS_PER_CHUNK]

    # no doc spec — just combinations
    return list(combinations(ids, 2))[:_MAX_PAIRS_PER_CHUNK]


def extract_kg_from_document(db: Session, document: models.Document, task: Optional[models.BackgroundTask] = None) -> dict:
    """
    Walk chunks, run extractor + classifier, write entities and relations.

    Returns a summary dict {entities, relations, chunks_processed, llm_calls}.
    Caller is responsible for db.commit() and task.status updates.
    """
    chunks: List[models.DocumentChunk] = (
        db.query(models.DocumentChunk)
        .filter_by(document_id=document.id)
        .order_by(models.DocumentChunk.chunk_index)
        .all()
    )

    if not chunks:
        return {"entities": 0, "relations": 0, "chunks_processed": 0, "llm_calls": 0}

    # First pass: extract all entities from all chunks, upsert as nodes.
    # We do this in a separate sweep to ensure entity rows exist before we
    # try to create relations referencing them.
    doc_spec = _find_doc_spec(document.title or "")
    doc_spec_id: Optional[str] = doc_spec.canonical_id if doc_spec else None

    canonical_to_entity: dict = {}
    if doc_spec:
        ent = kg_service.upsert_entity(
            db,
            canonical_id=doc_spec.canonical_id,
            type_=doc_spec.type,
            name=doc_spec.canonical_id,
            description=document.ai_summary,
        )
        canonical_to_entity[doc_spec.canonical_id] = ent

    chunk_specs_map: dict = {}
    for chunk in chunks:
        specs = kg_extractor.extract_specs(chunk.text)
        if not specs:
            continue
        chunk_specs_map[chunk.id] = specs
        for spec in specs:
            if spec.canonical_id in canonical_to_entity:
                continue
            ent = kg_service.upsert_entity(
                db,
                canonical_id=spec.canonical_id,
                type_=spec.type,
                name=spec.canonical_id,
            )
            canonical_to_entity[spec.canonical_id] = ent

    db.flush()

    # ── Structural pass (deterministic, NO LLM): 通用 schema 的 Document / Section
    #    + contains / part_of / references。doc-type 無關，領域差異走 meta.kind。
    import os as _os

    structural_edges = 0
    doc_canon = f"doc:{document.id}"
    doc_ent = kg_service.upsert_entity(
        db, canonical_id=doc_canon, type_="document",
        name=document.title or doc_canon, meta={"document_id": document.id},
    )

    use_headings = (
        bool(getattr(settings, "KG_HEADING_SECTIONS", True))
        and document.pdf_path and _os.path.exists(document.pdf_path)
    )
    section_ent: dict = {}
    doc_level_specs: set = set()
    if use_headings:
        # 版面/字體式標題偵測（kg_headings）：method/section 變成節點，
        # 章節編號帶 scope key（annex 子章節不撞主方法號），引用邊由行級歸屬而來。
        from . import kg_headings
        try:
            with open(document.pdf_path, "rb") as _f:
                headings, section_specs, doc_level_specs = kg_headings.build_section_spec_map(_f.read())
        except Exception as exc:
            logger.warning("kg_headings failed for %s, falling back to kg_structure: %s", document.id, exc)
            headings, section_specs = [], {}
            use_headings = False
        for h in headings:
            ent = kg_service.upsert_entity(
                db, canonical_id=f"{doc_canon}#{h.key}", type_=h.kind, name=h.title,
                meta={"kind": h.kind, "number": h.number, "page": h.page, "document_id": document.id},
            )
            section_ent[h.key] = ent
        for h in headings:
            ent = section_ent[h.key]
            if h.parent and h.parent in section_ent:
                rel_ok = kg_service.upsert_relation(db, src_id=ent.id, dst_id=section_ent[h.parent].id,
                                                    rel_type="part_of", document_id=document.id, confidence=1.0)
            else:
                rel_ok = kg_service.upsert_relation(db, src_id=doc_ent.id, dst_id=ent.id,
                                                    rel_type="contains", document_id=document.id, confidence=1.0)
            if rel_ok:
                structural_edges += 1
        # 標題節點寫好後立刻把章節標籤貼回 chunk。放在這裡而不是入庫流程更前面，
        # 是因為標籤要有 page 才能對位，而 page 只有在標題抽取完成後才知道。
        # 失敗不能影響 KG 建立本身 —— 章節標籤缺了只是退回頁碼判斷，不是致命問題。
        try:
            from . import section_path as _sp
            _sp.assign_section_paths(db, document.id, commit=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("section_path 貼標籤失敗 %s: %s", document.id, exc)

        for key, specs in section_specs.items():
            sent = section_ent.get(key)
            if not sent:
                continue
            for sp in specs:
                sp_ent = canonical_to_entity.get(sp)
                if sp_ent and kg_service.upsert_relation(db, src_id=sent.id, dst_id=sp_ent.id,
                                                         rel_type="references", document_id=document.id, confidence=1.0):
                    structural_edges += 1

    if not use_headings:
        # Fallback：純文字 regex 結構偵測（無 PDF 或關閉旗標時）。
        from . import kg_structure
        sections, doc_level_specs = kg_structure.extract_structure(chunks)
        for number, sec in sections.items():
            ent = kg_service.upsert_entity(
                db, canonical_id=f"{doc_canon}#{number}", type_="section", name=sec.title,
                meta={"kind": sec.kind, "number": number, "page": sec.page, "document_id": document.id},
            )
            section_ent[number] = ent
            if kg_service.upsert_relation(db, src_id=doc_ent.id, dst_id=ent.id, rel_type="contains",
                                          document_id=document.id, confidence=1.0):
                structural_edges += 1
        for number, ent in section_ent.items():
            parent = kg_structure._parent_number(number)
            if parent and parent in section_ent:
                if kg_service.upsert_relation(db, src_id=ent.id, dst_id=section_ent[parent].id,
                                              rel_type="part_of", document_id=document.id, confidence=1.0):
                    structural_edges += 1
        for number, sec in sections.items():
            for sp in sec.specs:
                sp_ent = canonical_to_entity.get(sp)
                if sp_ent and kg_service.upsert_relation(db, src_id=section_ent[number].id, dst_id=sp_ent.id,
                                                         rel_type="references", document_id=document.id, confidence=1.0):
                    structural_edges += 1

    for sp in doc_level_specs:
        sp_ent = canonical_to_entity.get(sp)
        if sp_ent and kg_service.upsert_relation(db, src_id=doc_ent.id, dst_id=sp_ent.id,
                                                 rel_type="references", document_id=document.id, confidence=1.0):
            structural_edges += 1
    db.commit()

    # Second pass: relation classification per chunk
    llm = get_llm_provider()

    def _llm_chat(messages, model=None, format=None):
        return llm.chat(messages, model=model, format=format)

    llm_calls = 0
    relations_added = 0
    total_chunks = len(chunks)
    processed = 0
    min_conf = float(getattr(settings, "KG_MIN_CONFIDENCE", 0.3) or 0.3)

    for chunk in chunks:
        processed += 1
        specs = chunk_specs_map.get(chunk.id)
        if not specs:
            continue
        if len(specs) < 2 and not doc_spec_id:
            # no relation can exist with only one spec and no doc-level identity
            continue

        pairs = _pick_pairs(specs, doc_spec_id)
        if not pairs:
            continue

        for src_canonical, dst_canonical in pairs:
            llm_calls += 1
            try:
                pred = classify_pair(
                    src_canonical,
                    dst_canonical,
                    chunk.text,
                    llm_chat=_llm_chat,
                )
            except Exception as e:
                logger.warning("kg classify failed (%s -> %s): %s", src_canonical, dst_canonical, e)
                continue
            if not pred or pred.confidence < min_conf:
                continue

            src_ent = canonical_to_entity.get(pred.src)
            dst_ent = canonical_to_entity.get(pred.dst)
            if not src_ent or not dst_ent:
                continue

            row = kg_service.upsert_relation(
                db,
                src_id=src_ent.id,
                dst_id=dst_ent.id,
                rel_type=pred.rel_type,
                document_id=document.id,
                chunk_id=chunk.id,
                confidence=pred.confidence,
                evidence=pred.evidence,
            )
            if row is not None:
                relations_added += 1

        # Update progress periodically (every 5 chunks)
        if task and processed % 5 == 0:
            try:
                task.progress = int(processed / total_chunks * 100)
                task.message = f"KG 抽取中 {processed}/{total_chunks}"
                db.commit()
            except Exception:
                db.rollback()

    db.commit()

    return {
        "entities": len(canonical_to_entity) + 1 + len(section_ent),  # specs + document + sections
        "relations": relations_added + structural_edges,
        "structural_relations": structural_edges,
        "sections": len(section_ent),
        "chunks_processed": processed,
        "llm_calls": llm_calls,
    }


def run_kg_extract_task(task_id: str, document_id: str) -> None:
    """FastAPI BackgroundTasks entry — independent DB session."""
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        task = db.query(models.BackgroundTask).filter_by(id=task_id).first()
        document = db.query(models.Document).filter_by(id=document_id).first()
        if not task or not document:
            return

        task.status = "running"
        task.message = "解析規範引用..."
        db.commit()

        # If re-running on a doc that already has KG rows, clear them so we
        # don't pile up duplicates with stale confidence.
        kg_service.delete_kg_for_document(db, document_id)
        db.commit()

        summary = extract_kg_from_document(db, document, task=task)

        task.status = "completed"
        task.progress = 100
        task.message = (
            f"KG 抽取完成：{summary['entities']} 個實體、{summary['relations']} 條關係"
        )
        task.result = summary
        db.commit()

    except Exception as exc:
        logger.error("KG extract task failed: %s", exc, exc_info=True)
        try:
            task = db.query(models.BackgroundTask).filter_by(id=task_id).first()
            if task:
                task.status = "failed"
                task.error = str(exc)[:500]
                db.commit()
        except Exception:
            pass
    finally:
        db.close()
