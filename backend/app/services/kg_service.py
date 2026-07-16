"""
KG read/write helpers — used by both API endpoints and the extraction
background task. Keeps SQL out of the routers.
"""
from __future__ import annotations

import logging
from typing import Dict, Iterable, List, Optional, Tuple

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from .. import models

logger = logging.getLogger(__name__)


def upsert_entity(
    db: Session,
    *,
    canonical_id: str,
    type_: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    meta: Optional[dict] = None,
) -> models.KGEntity:
    """Get-or-create a single entity by canonical_id. Caller commits.

    `meta` 存通用屬性（如 Section 的 kind / number / page），讓領域差異走屬性而非新增型別。
    """
    row = db.query(models.KGEntity).filter_by(canonical_id=canonical_id).first()
    if row:
        if description and not row.description:
            row.description = description
        if meta:
            merged = dict(row.meta or {})
            merged.update(meta)
            row.meta = merged
        return row
    row = models.KGEntity(
        canonical_id=canonical_id,
        type=type_,
        name=name or canonical_id,
        description=description,
        meta=meta or {},
    )
    db.add(row)
    db.flush()
    return row


def upsert_relation(
    db: Session,
    *,
    src_id: str,
    dst_id: str,
    rel_type: str,
    document_id: Optional[str] = None,
    chunk_id: Optional[str] = None,
    confidence: float = 1.0,
    evidence: Optional[str] = None,
) -> Optional[models.KGRelation]:
    """Insert relation if (src,dst,rel,doc) triple is not already present."""
    if src_id == dst_id:
        return None
    existing = (
        db.query(models.KGRelation)
        .filter_by(
            src_id=src_id,
            dst_id=dst_id,
            rel_type=rel_type,
            document_id=document_id,
        )
        .first()
    )
    if existing:
        # update confidence to the max we've seen
        if confidence > existing.confidence:
            existing.confidence = confidence
        return existing
    row = models.KGRelation(
        src_id=src_id,
        dst_id=dst_id,
        rel_type=rel_type,
        document_id=document_id,
        chunk_id=chunk_id,
        confidence=confidence,
        evidence=(evidence or "")[:2000],
    )
    db.add(row)
    db.flush()
    return row


def search_entities(
    db: Session,
    q: str,
    *,
    limit: int = 20,
) -> List[models.KGEntity]:
    """Search entities by canonical_id or name (case-insensitive substring)."""
    if not q or not q.strip():
        return []
    q_norm = q.strip()
    rows = (
        db.query(models.KGEntity)
        .filter(
            or_(
                models.KGEntity.canonical_id.ilike(f"%{q_norm}%"),
                models.KGEntity.name.ilike(f"%{q_norm}%"),
            )
        )
        .order_by(models.KGEntity.canonical_id)
        .limit(limit)
        .all()
    )
    return rows


def get_neighbors(
    db: Session,
    entity_id: str,
    *,
    hops: int = 1,
    limit_per_hop: int = 200,
) -> Tuple[List[models.KGEntity], List[models.KGRelation]]:
    """
    BFS up to `hops` from the center entity. Returns (nodes, edges) including
    the center. Hops bound + limit_per_hop bound to keep visualization sane.
    """
    if hops < 1:
        hops = 1
    if hops > 3:
        hops = 3

    visited_ids: set = {entity_id}
    frontier: List[str] = [entity_id]
    all_edges: List[models.KGRelation] = []

    for _hop in range(hops):
        if not frontier:
            break
        edges = (
            db.query(models.KGRelation)
            .filter(
                or_(
                    models.KGRelation.src_id.in_(frontier),
                    models.KGRelation.dst_id.in_(frontier),
                )
            )
            .limit(limit_per_hop)
            .all()
        )
        new_frontier: List[str] = []
        for edge in edges:
            all_edges.append(edge)
            for nid in (edge.src_id, edge.dst_id):
                if nid not in visited_ids:
                    visited_ids.add(nid)
                    new_frontier.append(nid)
        frontier = new_frontier

    nodes = db.query(models.KGEntity).filter(models.KGEntity.id.in_(visited_ids)).all()
    # dedup edges
    seen_edges: set = set()
    dedup_edges: List[models.KGRelation] = []
    for e in all_edges:
        key = (e.src_id, e.dst_id, e.rel_type, e.document_id)
        if key in seen_edges:
            continue
        seen_edges.add(key)
        dedup_edges.append(e)
    return nodes, dedup_edges


def get_supersedes_chain(
    db: Session,
    entity_id: str,
    *,
    max_depth: int = 10,
) -> List[models.KGEntity]:
    """
    Walk the supersedes chain: starting entity, then everything it supersedes,
    then everything that supersedes it (in display order: oldest → newest).
    """
    seen: set = set()
    # Walk backward: entities this one supersedes (oldest)
    older: List[models.KGEntity] = []
    cursor_id = entity_id
    for _ in range(max_depth):
        edge = (
            db.query(models.KGRelation)
            .filter_by(src_id=cursor_id, rel_type="supersedes")
            .first()
        )
        if not edge or edge.dst_id in seen:
            break
        seen.add(edge.dst_id)
        ent = db.query(models.KGEntity).filter_by(id=edge.dst_id).first()
        if not ent:
            break
        older.insert(0, ent)
        cursor_id = edge.dst_id

    # Walk forward: entities superseding this one (newer)
    newer: List[models.KGEntity] = []
    cursor_id = entity_id
    for _ in range(max_depth):
        edge = (
            db.query(models.KGRelation)
            .filter_by(dst_id=cursor_id, rel_type="supersedes")
            .first()
        )
        if not edge or edge.src_id in seen:
            break
        seen.add(edge.src_id)
        ent = db.query(models.KGEntity).filter_by(id=edge.src_id).first()
        if not ent:
            break
        newer.append(ent)
        cursor_id = edge.src_id

    center = db.query(models.KGEntity).filter_by(id=entity_id).first()
    chain = older[:]
    if center:
        chain.append(center)
    chain.extend(newer)
    return chain


def delete_kg_for_document(db: Session, document_id: str) -> int:
    """Remove all KG relations sourced from a given document. Returns count.

    Shared spec entities (ISO/MIL-STD/…) are kept — they may be mentioned in
    other docs. But this document's OWN structural entities
    (canonical_id like ``doc:{id}`` / ``doc:{id}#510.7``) are single-document by
    nature, so they are purged here to avoid orphan/ghost nodes on delete and
    stale nodes on re-extract (audit H5 / orphan-section fix). They are rebuilt
    when extraction re-runs.
    """
    rows = (
        db.query(models.KGRelation)
        .filter_by(document_id=document_id)
        .all()
    )
    n = len(rows)
    for r in rows:
        db.delete(r)

    # purge this document's own structural entities (doc:{id} and doc:{id}#...)
    prefix = f"doc:{document_id}"
    (
        db.query(models.KGEntity)
        .filter(
            (models.KGEntity.canonical_id == prefix)
            | (models.KGEntity.canonical_id.like(prefix + "#%"))
        )
        .delete(synchronize_session=False)
    )
    return n
