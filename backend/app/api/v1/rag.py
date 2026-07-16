import os
import json
import re
from typing import Dict, List
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ... import models, schemas
from ...core.config import settings
from ...core.security import get_current_user
from ...database import get_db
from ...services import ai, pdf_image, rerank, retrieval
from ...services.system_config import SystemConfigService


router = APIRouter()


def _hybrid_filtered(db, payload, search_query, embedding, vector_config, top_k):
    """Thin adapter → services.retrieval.hybrid_retrieve（檢索邏輯的唯一實作）。"""
    return retrieval.hybrid_retrieve(
        db, search_query, embedding, top_k,
        vector_config=vector_config,
        document_id=payload.document_id,
        classification_id=payload.classification_id,
        project_id=payload.project_id,
        folder_ids=payload.folder_ids,
    )


def _context_keep_ids(filtered):
    return retrieval.context_keep_ids(filtered)


def _context_text_budgeted(db: Session, chunk, ctx_used: int) -> str:
    return retrieval.context_text_budgeted(db, chunk, ctx_used)


@router.post("/query", response_model=schemas.RAGQueryResponse)
def query_rag(
    payload: schemas.RAGQueryRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    question = (payload.question or "").strip()
    if not question:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="問題不可為空")

    config_service = SystemConfigService(db)
    vector_config = config_service.get_vector_config()

    if payload.use_ai_fallback:
        fallback_answer = ai.generate_fallback_answer(question)
        return schemas.RAGQueryResponse(
            answer=fallback_answer, sources=[], used_ai_fallback=True
        )

    followup_analysis = None
    search_query = question
    is_followup = False
    optimized_query = None

    if payload.conversation_history and not payload.skip_ai_understanding:
        history = [
            {"question": m.question, "answer": m.answer}
            for m in payload.conversation_history
        ]
        followup_analysis = ai.analyze_followup_intent(question, history)
        is_followup = bool(followup_analysis.get("is_followup", False))
        needs_new_search = bool(followup_analysis.get("needs_new_search", True))
        optimized_query = followup_analysis.get("optimized_query", question) or question

        # 單一精簡查詢；強制與原始語言一致
        search_query = optimized_query
        has_cn = bool(re.search(r"[\u4e00-\u9fff]", question))
        has_cn_opt = bool(re.search(r"[\u4e00-\u9fff]", optimized_query))
        if (has_cn and not has_cn_opt) or (not has_cn and has_cn_opt):
            search_query = question

        # Always re-search with optimized_query for accurate follow-up results

    embeddings = ai.embed_texts([search_query])
    if not embeddings:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="嵌入計算失敗")

    filtered = _hybrid_filtered(
        db, payload, search_query, embeddings[0], vector_config, payload.top_k
    )
    if not filtered:
        return schemas.RAGQueryResponse(answer="找不到符合的內容，請調整關鍵詞或過濾條件", sources=[])

    # 頁距過濾（智慧 + 塌縮保護）：命中集中才套用；分散/主命中離群則全納入。
    primary_page = filtered[0][0].page or 0
    keep_ids = _context_keep_ids(filtered)

    contexts: List[Dict[str, str]] = []
    sources: List[schemas.DocumentChunkSource] = []
    ctx_used = 0
    for chunk, score in filtered:
        doc = chunk.document
        chunk_page = chunk.page or 0
        page_gap = abs(chunk_page - primary_page) if primary_page and chunk_page else None

        source_num = len(sources) + 1  # 1-based index matching frontend display order
        sources.append(
            schemas.DocumentChunkSource(
                document_id=doc.id,
                title=doc.title,
                page=chunk.page,
                snippet=chunk.text,
                score=score,
            )
        )

        if chunk.id not in keep_ids:
            # 加入 sources 讓前端顯示，但不傳入 LLM context
            continue

        text = _context_text_budgeted(db, chunk, ctx_used)
        ctx_used += len(text)
        contexts.append({
            "source_num": source_num,
            "title": doc.title,
            "page": chunk_page,
            "page_gap": page_gap,
            "text": text,
        })

    final_question = optimized_query or question

    # 低信心 fallback：最相關段落的 cross-encoder 分數都低於門檻 → 不硬答，
    # 改回「最接近內容 + LLM 動態產生的查詢修正建議」（CE 不可用時 conf=None，照常作答）。
    conf = rerank.top_relevance(search_query, [c for c, _ in filtered])
    if conf is not None and conf < getattr(settings, "RAG_LOWCONF_CE_THRESHOLD", 0.15):
        closest = contexts or [
            {"title": s.title, "page": s.page, "text": s.snippet} for s in sources[:3]
        ]
        return schemas.RAGQueryResponse(
            answer=ai.low_confidence_answer(final_question, closest),
            sources=sources,
            is_followup=is_followup,
            optimized_query=optimized_query,
            low_confidence=True,
        )

    history = (
        [{"question": m.question, "answer": m.answer} for m in payload.conversation_history]
        if payload.conversation_history else None
    )
    rag_prompts = config_service.get_rag_prompts()
    answer = ai.generate_rag_answer(
        final_question, contexts, conversation_history=history,
        system_prompt=rag_prompts["system_prompt"],
        user_template=rag_prompts["user_template"],
    )
    return schemas.RAGQueryResponse(
        answer=answer,
        sources=sources,
        is_followup=is_followup,
        optimized_query=optimized_query,
        suggested_questions=[],
    )


@router.post("/query/stream")
def query_stream(
    payload: schemas.RAGQueryRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """與 /query 相同的檢索邏輯，但以 SSE 串流回傳思考過程與答案。"""
    question = (payload.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="問題不可為空白")

    config_service = SystemConfigService(db)
    vector_config = config_service.get_vector_config()

    is_followup = False
    optimized_query = None
    if payload.conversation_history and not payload.skip_ai_understanding:
        try:
            history_dicts = [{"question": m.question, "answer": m.answer} for m in payload.conversation_history]
            intent = ai.analyze_followup_intent(question, history_dicts)
            is_followup = bool(intent.get("is_followup", False))
            optimized_query = intent.get("optimized_query") or question
        except Exception:
            optimized_query = question
    else:
        optimized_query = question

    search_query = optimized_query or question
    has_cn = bool(re.search(r"[\u4e00-\u9fff]", question))
    has_cn_opt = bool(re.search(r"[\u4e00-\u9fff]", search_query))
    if (has_cn and not has_cn_opt) or (not has_cn and has_cn_opt):
        search_query = question

    embeddings = ai.embed_texts([search_query])
    if not embeddings:
        def _embed_error():
            yield f"data: {json.dumps({'type': 'error', 'message': '嵌入計算失敗'}, ensure_ascii=False)}\n\n"
        return StreamingResponse(_embed_error(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    top_k = payload.top_k or 5
    filtered = _hybrid_filtered(
        db, payload, search_query, embeddings[0], vector_config, top_k
    )

    if not filtered:
        def _no_match():
            yield f"data: {json.dumps({'type': 'content', 'text': '找不到符合的內容，請調整關鍵詞或過濾條件'}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'sources', 'sources': [], 'is_followup': is_followup, 'optimized_query': optimized_query}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
        return StreamingResponse(_no_match(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    primary_page = filtered[0][0].page or 0
    keep_ids = _context_keep_ids(filtered)

    contexts: List[Dict[str, str]] = []
    sources: List[schemas.DocumentChunkSource] = []
    ctx_used = 0
    for chunk, score in filtered:
        doc = chunk.document
        chunk_page = chunk.page or 0
        page_gap = abs(chunk_page - primary_page) if primary_page and chunk_page else None
        source_num = len(sources) + 1
        sources.append(schemas.DocumentChunkSource(
            document_id=doc.id, title=doc.title, page=chunk.page,
            snippet=chunk.text, score=score,
        ))
        if chunk.id not in keep_ids:
            continue
        text = _context_text_budgeted(db, chunk, ctx_used)
        ctx_used += len(text)
        contexts.append({
            "source_num": source_num, "title": doc.title, "page": chunk_page, "page_gap": page_gap,
            "text": text,
        })

    final_question = optimized_query or question
    history = (
        [{"question": m.question, "answer": m.answer} for m in payload.conversation_history]
        if payload.conversation_history else None
    )
    sources_data = [s.model_dump() for s in sources]
    rag_prompts = config_service.get_rag_prompts()

    def generate():
        try:
            if not contexts:
                yield f"data: {json.dumps({'type': 'content', 'text': '查無足夠的相關內容，請提供更多文件或調整問題。'}, ensure_ascii=False)}\n\n"
            else:
                for stream_chunk in ai.generate_rag_answer_stream(
                    final_question, contexts, conversation_history=history,
                    system_prompt=rag_prompts["system_prompt"],
                    user_template=rag_prompts["user_template"],
                ):
                    yield f"data: {json.dumps(stream_chunk, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'sources', 'sources': sources_data, 'is_followup': is_followup, 'optimized_query': optimized_query}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse_event(event: str, data: Dict[str, str]) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


@router.post("/analyze-pdf-pages", response_model=schemas.PdfPageAnalysisResponse)
async def analyze_pdf_pages(
    payload: schemas.PdfPageAnalysisRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    # Audit M：VL 多頁分析是最重的 LLM 呼叫之一 → 每人配額
    from ...utils.ratelimit import check_quota
    check_quota(current_user.id, "vl_analyze", limit=30, per_seconds=3600)

    document = db.query(models.Document).filter(models.Document.id == payload.document_id).first()
    if not document:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文件不存在")
    if not document.pdf_path:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="此文件沒有可用的 PDF")

    pdf_full_path = document.pdf_path
    if not os.path.exists(pdf_full_path):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PDF 檔案不存在")

    total_pages = pdf_image.get_pdf_page_count(pdf_full_path)
    invalid = [p for p in payload.page_numbers if p < 1 or p > total_pages]
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"頁碼 {invalid} 超出文件總頁數 {total_pages}",
        )

    unique_pages = list(dict.fromkeys(payload.page_numbers))
    if len(unique_pages) > settings.MAX_PDF_ANALYSIS_PAGES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"一次最多分析 {settings.MAX_PDF_ANALYSIS_PAGES} 頁，請減少頁數後重試",
        )
    if len(unique_pages) > 1:
        image_bytes_list = pdf_image.pdf_pages_to_images(pdf_full_path, unique_pages, dpi=100, max_dimension=1024)
    else:
        image_bytes_list = pdf_image.pdf_pages_to_images(pdf_full_path, unique_pages)
    history = (
        [{"question": m.question, "answer": m.answer} for m in payload.conversation_history]
        if payload.conversation_history
        else None
    )
    answer = ai.analyze_pdf_page_images_singleturn(
        image_bytes_list=image_bytes_list,
        page_numbers=unique_pages,
        question=payload.question,
        conversation_history=history,
    )
    return schemas.PdfPageAnalysisResponse(
        answer=answer, page_numbers=unique_pages, document_title=document.title or ""
    )


@router.post("/analyze-pdf-pages/stream")
def analyze_pdf_pages_stream(
    payload: schemas.PdfPageAnalysisRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    # Audit M：與非串流版共用同一配額 key
    from ...utils.ratelimit import check_quota
    check_quota(current_user.id, "vl_analyze", limit=30, per_seconds=3600)

    document = db.query(models.Document).filter(models.Document.id == payload.document_id).first()
    if not document:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文件不存在")
    if not document.pdf_path:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="此文件沒有可用的 PDF")

    pdf_full_path = document.pdf_path
    if not os.path.exists(pdf_full_path):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PDF 檔案不存在")

    total_pages = pdf_image.get_pdf_page_count(pdf_full_path)
    invalid = [p for p in payload.page_numbers if p < 1 or p > total_pages]
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"頁碼 {invalid} 超出文件總頁數 {total_pages}",
        )

    unique_pages = list(dict.fromkeys(payload.page_numbers))
    if len(unique_pages) > settings.MAX_PDF_ANALYSIS_PAGES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"一次最多分析 {settings.MAX_PDF_ANALYSIS_PAGES} 頁，請減少頁數後重試",
        )
    # 多頁分析降低解析度避免 context overflow；單頁維持原始高解析度
    if len(unique_pages) > 1:
        image_bytes_list = pdf_image.pdf_pages_to_images(pdf_full_path, unique_pages, dpi=100, max_dimension=1024)
    else:
        image_bytes_list = pdf_image.pdf_pages_to_images(pdf_full_path, unique_pages)
    history = (
        [{"question": m.question, "answer": m.answer} for m in payload.conversation_history]
        if payload.conversation_history
        else None
    )

    def event_gen():
        try:
            content_accum: List[str] = []
            thinking_accum: List[str] = []
            for delta in ai.analyze_pdf_page_images_stream(
                image_bytes_list=image_bytes_list,
                page_numbers=unique_pages,
                question=payload.question,
                conversation_history=history,
            ):
                if not isinstance(delta, dict):
                    continue
                t = delta.get("type")
                text = delta.get("text") or ""
                if not text:
                    continue
                if t == "thinking":
                    thinking_accum.append(text)
                    yield _sse_event("thinking", {"text": text})
                else:
                    content_accum.append(text)
                    yield _sse_event("content", {"text": text})

            final_content = "".join(content_accum).strip()
            if len(final_content) < 20:
                notes = "".join(thinking_accum).strip()
                if notes:
                    final_answer = ai.finalize_text_answer(notes, payload.question)
                    if final_answer and final_answer.strip():
                        final_content = final_answer
                        yield _sse_event("content", {"text": final_answer})
            
            # Save conversation history to per-user DB table
            try:
                question_label = payload.question
                if not question_label:
                    if len(unique_pages) == 1:
                        question_label = f"請分析第 {unique_pages[0]} 頁的重點內容"
                    else:
                        question_label = f"請分析第 {', '.join(map(str, unique_pages))} 頁的整體內容"

                new_entry = {
                    "question": question_label,
                    "answer": final_content,
                    "page_numbers": unique_pages,
                    "timestamp": str(datetime.now()),
                }

                row = db.query(models.DocumentUserAnalysis).filter(
                    models.DocumentUserAnalysis.document_id == payload.document_id,
                    models.DocumentUserAnalysis.user_id == current_user.id,
                ).first()
                if row:
                    from sqlalchemy.orm.attributes import flag_modified
                    row.messages = row.messages + [new_entry]
                    flag_modified(row, "messages")
                else:
                    row = models.DocumentUserAnalysis(
                        document_id=payload.document_id,
                        user_id=current_user.id,
                        messages=[new_entry],
                    )
                    db.add(row)
                db.commit()
            except Exception as db_err:
                print(f"Failed to save conversation history: {db_err}")
                # Don't fail the stream for DB error, just log it

        except Exception as e:
            yield _sse_event("error", {"message": str(e)})

    return StreamingResponse(event_gen(), media_type="text/event-stream")


# ===== Per-user conversation history =====

@router.get("/config")
def get_rag_config(current_user=Depends(get_current_user)):
    """回傳前端需要的 RAG 相關設定值"""
    return {"max_pdf_analysis_pages": settings.MAX_PDF_ANALYSIS_PAGES}


@router.get("/conversation")
def get_conversation(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """取得當前使用者的對話紀錄"""
    row = db.query(models.UserConversation).filter(
        models.UserConversation.user_id == current_user.id
    ).first()
    return {"messages": row.messages if row else []}


@router.put("/conversation")
def save_conversation(
    payload: dict,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """儲存當前使用者的對話紀錄"""
    messages = payload.get("messages", [])
    row = db.query(models.UserConversation).filter(
        models.UserConversation.user_id == current_user.id
    ).first()
    if row:
        row.messages = messages
    else:
        row = models.UserConversation(user_id=current_user.id, messages=messages)
        db.add(row)
    db.commit()
    return {"ok": True}


@router.delete("/conversation", status_code=204)
def clear_conversation(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """清除當前使用者的對話紀錄"""
    db.query(models.UserConversation).filter(
        models.UserConversation.user_id == current_user.id
    ).delete()
    db.commit()
    return None

