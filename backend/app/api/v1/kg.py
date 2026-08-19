"""
Knowledge graph query API. Used by the visualization page and by the Agent
tools layer (services/agent_tools.py).
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ... import models, schemas
from ...core.security import get_current_user
from ...database import get_db
from ...services import kg_auto, kg_pipeline, kg_service, kg_tables

router = APIRouter()


def _entity_to_read(e: models.KGEntity) -> schemas.KGEntityRead:
    return schemas.KGEntityRead.model_validate(e)


@router.get("/entities/search", response_model=List[schemas.KGEntityRead])
def search_entities(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    rows = kg_service.search_entities(db, q, limit=limit)
    return [_entity_to_read(r) for r in rows]


@router.get("/entities/{entity_id}", response_model=schemas.KGEntityRead)
def get_entity(
    entity_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    row = db.query(models.KGEntity).filter_by(id=entity_id).first()
    if not row:
        # accept canonical_id too
        row = db.query(models.KGEntity).filter_by(canonical_id=entity_id).first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="entity not found")
    return _entity_to_read(row)


@router.get("/entities/{entity_id}/neighbors", response_model=schemas.KGNeighborsResponse)
def get_entity_neighbors(
    entity_id: str,
    hops: int = Query(1, ge=1, le=3),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    center = db.query(models.KGEntity).filter_by(id=entity_id).first()
    if not center:
        center = db.query(models.KGEntity).filter_by(canonical_id=entity_id).first()
    if not center:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="entity not found")

    nodes, edges = kg_service.get_neighbors(db, center.id, hops=hops)
    node_map = {n.id: n for n in nodes}

    neighbors: List[schemas.KGEntityNeighbor] = []
    for edge in edges:
        if edge.src_id == center.id and edge.dst_id != center.id:
            other = node_map.get(edge.dst_id)
            direction = "out"
        elif edge.dst_id == center.id and edge.src_id != center.id:
            other = node_map.get(edge.src_id)
            direction = "in"
        else:
            continue
        if not other:
            continue
        neighbors.append(
            schemas.KGEntityNeighbor(
                entity=_entity_to_read(other),
                rel_type=edge.rel_type,
                direction=direction,
                confidence=edge.confidence,
                document_id=edge.document_id,
            )
        )

    return schemas.KGNeighborsResponse(
        center=_entity_to_read(center),
        neighbors=neighbors,
    )


@router.get("/entities/{entity_id}/supersedes-chain", response_model=List[schemas.KGEntityRead])
def get_supersedes_chain(
    entity_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    center = db.query(models.KGEntity).filter_by(id=entity_id).first()
    if not center:
        center = db.query(models.KGEntity).filter_by(canonical_id=entity_id).first()
    if not center:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="entity not found")
    chain = kg_service.get_supersedes_chain(db, center.id)
    return [_entity_to_read(e) for e in chain]


@router.get("/graph", response_model=schemas.KGGraphResponse)
def get_graph(
    center: Optional[str] = Query(None, description="entity id or canonical_id; omit for full graph"),
    hops: int = Query(2, ge=1, le=3),
    limit: int = Query(500, ge=10, le=2000),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Return a slice of the graph for visualization."""
    if center:
        ent = db.query(models.KGEntity).filter_by(id=center).first()
        if not ent:
            ent = db.query(models.KGEntity).filter_by(canonical_id=center).first()
        if not ent:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="center entity not found")
        nodes, edges = kg_service.get_neighbors(db, ent.id, hops=hops, limit_per_hop=limit)
    else:
        # full-graph slice — bound by `limit` edges, then resolve nodes
        edges = db.query(models.KGRelation).limit(limit).all()
        node_ids = {e.src_id for e in edges} | {e.dst_id for e in edges}
        nodes = db.query(models.KGEntity).filter(models.KGEntity.id.in_(node_ids)).all() if node_ids else []

    return schemas.KGGraphResponse(
        nodes=[
            schemas.KGGraphNode(
                id=n.id,
                canonical_id=n.canonical_id,
                name=n.name,
                type=n.type,
            )
            for n in nodes
        ],
        edges=[
            schemas.KGGraphEdge(
                src_id=e.src_id,
                dst_id=e.dst_id,
                rel_type=e.rel_type,
                confidence=e.confidence,
            )
            for e in edges
        ],
    )


@router.post("/extract/{document_id}", status_code=status.HTTP_202_ACCEPTED)
def extract_for_document(
    document_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Kick off KG extraction for a single document. Returns the background task ID."""
    # Audit M：重運算端點配額（KG 抽取=大量 LLM 呼叫，會排爆序列 worker）
    from ...utils.ratelimit import check_quota
    check_quota(current_user.id, "kg_extract", limit=20, per_seconds=3600)

    document = db.query(models.Document).filter_by(id=document_id).first()
    if not document:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文件不存在")

    # 同文件已有 pending/running 的 KG 任務 → 直接回傳既有任務，不重複入列
    existing = (
        db.query(models.BackgroundTask)
        .filter(
            models.BackgroundTask.task_type == "kg_extract",
            models.BackgroundTask.document_id == document_id,
            models.BackgroundTask.status.in_(("pending", "running")),
        )
        .first()
    )
    if existing:
        from ...services import kg_queue
        return {"task_id": existing.id, "document_id": document_id,
                "queue_depth": kg_queue.queue_depth(), "deduplicated": True}

    task = models.BackgroundTask(
        task_type="kg_extract",
        status="pending",
        progress=0,
        message="等待 KG 抽取開始...",
        document_id=document_id,
        creator_id=current_user.id,
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    # serialise through the single KG worker (avoids SQLite write-lock contention
    # when many docs are queued at once); task shows as pending until the worker reaches it
    from ...services import kg_queue
    depth = kg_queue.enqueue(task.id, document_id)
    return {"task_id": task.id, "document_id": document_id, "queue_depth": depth}


@router.delete("/document/{document_id}/relations", status_code=204)
def delete_document_relations(
    document_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Remove all KG relations sourced from a document. Entities are kept."""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="僅管理員可刪除 KG 關係")
    n = kg_service.delete_kg_for_document(db, document_id)
    db.commit()
    return None


@router.get("/stats")
def get_stats(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    total_entities = db.query(models.KGEntity).count()
    total_relations = db.query(models.KGRelation).count()
    type_breakdown_rows = (
        db.query(models.KGEntity.type, models.KGEntity.id)
        .all()
    )
    type_counts: dict = {}
    for t, _ in type_breakdown_rows:
        type_counts[t] = type_counts.get(t, 0) + 1
    rel_breakdown_rows = (
        db.query(models.KGRelation.rel_type, models.KGRelation.id)
        .all()
    )
    rel_counts: dict = {}
    for t, _ in rel_breakdown_rows:
        rel_counts[t] = rel_counts.get(t, 0) + 1
    return {
        "total_entities": total_entities,
        "total_relations": total_relations,
        "type_counts": type_counts,
        "rel_counts": rel_counts,
    }


# ── 表格關係抽取 ────────────────────────────────────────────────────
# 現有的實體抽取只認規範編號（regex）。語料裡真正的結構資訊多半在表格 ——
# 教育訓練文件 70% 的塊含表格，卻在 KG 裡只有 1 個節點。這組端點讓使用者
# 指定「哪張表、哪兩欄是代號與名稱」，逐列建成節點與 contains/part_of 邊，
# 之後 list_subitems 就能完整列舉（列舉題不必再賭向量排序）。
#
# 流程刻意是「偵測 → 乾跑預覽 → 確認才寫入 → 可整批回退」：抽取器一旦寫錯
# 會靜默污染整張圖，看不到結果就上線是這個專案吃過最多虧的模式。

@router.get("/tables/{document_id}")
def list_document_tables(
    document_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """列出文件中所有可抽取的表格（含表頭與前 5 列樣本）。唯讀。"""
    _ = current_user
    if not db.query(models.Document).filter_by(id=document_id).first():
        raise HTTPException(status_code=404, detail="找不到文件")
    return {"tables": kg_tables.detect_tables(db, document_id)}


@router.post("/tables/{document_id}/preview")
def preview_table_extraction(
    document_id: str,
    spec: dict,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """乾跑：回報「會建立什麼」，不寫入任何東西。"""
    _ = current_user
    plan = kg_tables.build_plan(db, document_id, spec)
    if plan.get("error"):
        raise HTTPException(status_code=400, detail=plan["error"])
    return plan


@router.post("/tables/{document_id}/apply", status_code=status.HTTP_201_CREATED)
def apply_table_extraction(
    document_id: str,
    spec: dict,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """確認後實際寫入 KG。"""
    _ = current_user
    res = kg_tables.apply_plan(db, document_id, spec)
    if res.get("error"):
        raise HTTPException(status_code=400, detail=res["error"])
    db.commit()
    return res


@router.post("/tables/{document_id}/auto-suggest")
def auto_suggest_tables(
    document_id: str,
    body: Optional[dict] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """AI 自動建議：LLM 判斷每張表值不值得抽、母節點名稱與欄位對映，
    規則引擎乾跑驗證。只讀不寫。body 可帶 {"model": "qwen3.8:27b"}。"""
    _ = current_user
    if not db.query(models.Document).filter_by(id=document_id).first():
        raise HTTPException(status_code=404, detail="找不到文件")
    return kg_auto.suggest(db, document_id, (body or {}).get("model"))


@router.post("/tables/{document_id}/auto-apply", status_code=status.HTTP_201_CREATED)
def auto_apply_tables(
    document_id: str,
    body: Optional[dict] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """套用 AI 建議：verdict=auto 的自動寫入；body.keys 指定的表（使用者看過
    預覽放行）即使 review 也寫入。body 可帶 {"model": ..., "keys": [...]}。"""
    _ = current_user
    if not db.query(models.Document).filter_by(id=document_id).first():
        raise HTTPException(status_code=404, detail="找不到文件")
    body = body or {}
    res = kg_auto.auto_apply(db, document_id, body.get("model"), body.get("keys"))
    db.commit()
    return res


@router.post("/citations/{document_id}/preview")
def preview_citations(
    document_id: str,
    body: Optional[dict] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """LLM 引用抽取「試抽」：限定頁碼範圍同步跑，回報查核後的引用清單，不寫入。
    body: {"model": ..., "page_lo": int, "page_hi": int}（範圍必填，全書請走背景任務）。"""
    _ = current_user
    body = body or {}
    if body.get("page_lo") is None or body.get("page_hi") is None:
        raise HTTPException(status_code=400, detail="試抽需指定 page_lo / page_hi（全書請用背景抽取）")
    if int(body["page_hi"]) - int(body["page_lo"]) > 40:
        raise HTTPException(status_code=400, detail="試抽範圍請在 40 頁以內")
    if not db.query(models.Document).filter_by(id=document_id).first():
        raise HTTPException(status_code=404, detail="找不到文件")
    return kg_auto.extract_citations(db, document_id, body.get("model"),
                                     int(body["page_lo"]), int(body["page_hi"]), apply=False)


@router.post("/citations/{document_id}", status_code=status.HTTP_202_ACCEPTED)
def start_citation_extraction(
    document_id: str,
    background_tasks: BackgroundTasks,
    body: Optional[dict] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """全書 LLM 引用抽取（背景任務）。入庫流程刻意不做這件事 —— 大文件要幾十分鐘，
    由管理者對特定文件手動啟動。body: {"model": "qwen3.8:27b"}。"""
    from ...utils.ratelimit import check_quota
    check_quota(current_user.id, "kg_citation", limit=10, per_seconds=3600)
    if not db.query(models.Document).filter_by(id=document_id).first():
        raise HTTPException(status_code=404, detail="找不到文件")
    existing = (db.query(models.BackgroundTask)
                .filter(models.BackgroundTask.task_type == "kg_citation",
                        models.BackgroundTask.document_id == document_id,
                        models.BackgroundTask.status.in_(("pending", "running"))).first())
    if existing:
        return {"task_id": existing.id, "document_id": document_id, "deduplicated": True}
    task = models.BackgroundTask(task_type="kg_citation", status="pending", progress=0,
                                 message="等待 LLM 引用抽取開始...",
                                 document_id=document_id, creator_id=current_user.id)
    db.add(task); db.commit(); db.refresh(task)
    background_tasks.add_task(kg_auto.run_citation_task, task.id, document_id,
                              (body or {}).get("model"))
    return {"task_id": task.id, "document_id": document_id}


@router.delete("/citations/{document_id}", status_code=200)
def remove_citations(
    document_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """整批回退 LLM 引用抽取建立的邊（規範實體保留）。"""
    _ = current_user
    n = kg_auto.remove_citations(db, document_id)
    db.commit()
    return {"n_relations": n}


@router.delete("/tables/extraction/{parent_name}", status_code=200)
def remove_table_extraction(
    parent_name: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """整批回退一次抽取（刪母節點與其所有子節點及邊）。"""
    _ = current_user
    res = kg_tables.remove_plan(db, parent_name)
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail=res.get("error") or "回退失敗")
    db.commit()
    return res
