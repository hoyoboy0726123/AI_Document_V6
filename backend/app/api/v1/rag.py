import os
import json
import logging
import re
from typing import Dict, List
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from ... import models, schemas
from ...core.config import settings
from ...core.security import get_current_user
from ...database import get_db
from ...services import agent, ai, conversations, pdf_image, rerank, retrieval
from ...services.system_config import SystemConfigService


logger = logging.getLogger(__name__)

router = APIRouter()


def _hybrid_filtered(db, payload, search_query, embedding, vector_config, top_k,
                     document_id=None):
    """Thin adapter → services.retrieval.hybrid_retrieve（檢索邏輯的唯一實作）。

    document_id 若有傳入則覆寫 payload 的值 —— 用於「查詢裡點名了規範編號」時
    把範圍鎖到該文件（見 retrieval.resolve_spec_scope）。
    """
    return retrieval.hybrid_retrieve(
        db, search_query, embedding, top_k,
        vector_config=vector_config,
        document_id=document_id or payload.document_id,
        classification_id=payload.classification_id,
        project_id=payload.project_id,
        folder_ids=payload.folder_ids,
    )


def _augment_for_values(db, payload, embedding_fn, vector_config, top_k, question, filtered):
    """問題在要數值、但檢索到的段落一個數值都沒有 → 先補一輪再生成。

    為什麼要有這道：規範的「INFORMATION REQUIRED」章節只列參數名稱，實際數值在
    別節。中文查詢常只命中前者，模型便交出一張每列都寫「需依測試計畫指定」的表格
    —— 實測沙塵測試就是如此，17 列有 16 列沒有內容。

    提示詞裡已有「嚴禁造出沒有內容的表格」，但模型不遵守，所以改用確定性做法。
    這裡刻意檢查「證據」而非「答案」：串流一旦開始就收不回來，事後才發現沒有數值
    已經來不及；而且檢查證據只多一輪檢索，不需要額外的 LLM 生成，純 RAG 的速度
    定位得以維持。

    補查關鍵字取自證據中的英文參數名稱與表號 —— 實測中文查詢撈不到任何數值段落，
    同一份文件用英文術語查卻立刻命中（沙塵：頁 271/275/276 的濃度、風速、時間）。
    """
    if not filtered or not agent.wants_values(question):
        return filtered
    if any(agent.has_values(c.text) for c, _ in filtered):
        return filtered

    evidence = [{"snippet": c.text} for c, _ in filtered]
    terms = agent.value_seed_terms(evidence)
    if not terms:
        return filtered

    seen = {c.id for c, _ in filtered}
    # 補查必須限制在「原命中所在的文件與頁碼範圍」內。沒有這道約束時，
    # 查沙塵測試會撈回黴菌測試（Method 508）的培養溫度與礦物鹽配方，
    # 答案讀起來完整卻是別的方法的條件 —— 比空表格危險得多。
    scope = agent.evidence_scope([(c.document_id, c.page) for c, _ in filtered])
    added = []
    for term in terms[:3]:
        emb = embedding_fn(f"{term} value requirement")
        if not emb:
            continue
        for c, s in _hybrid_filtered(db, payload, f"{term} value requirement", emb,
                                     vector_config, top_k):
            if c.id in seen or not agent.has_values(c.text):
                continue
            if not agent.in_scope(scope, c.document_id, c.page):
                continue
            seen.add(c.id)
            added.append((c, s))
    if not added:
        return filtered
    logger.info("RAG 補查數值：關鍵字 %s → 補入 %d 段", terms[:3], len(added))
    # 帶數值的放最前面：context 有預算上限，接在尾端會被截掉。
    return added + filtered


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

    # 規範編號從「嵌入用字串」移到「文件過濾條件」：它在目錄／頁首／參考文獻
    # 出現上千次，留在查詢裡會主導向量、把答案段落擠出候選池。
    vector_query, spec_doc_id = retrieval.resolve_spec_scope(db, search_query)
    embeddings = ai.embed_query(vector_query)
    if not embeddings:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="嵌入計算失敗")

    filtered = _hybrid_filtered(
        db, payload, search_query, embeddings[0], vector_config, payload.top_k,
        document_id=spec_doc_id,
    )
    filtered = _augment_for_values(
        db, payload, lambda q: (ai.embed_query(q) or [None])[0],
        vector_config, payload.top_k, question, filtered,
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
        # 預算用完時會回空字串。原本仍無條件 append，於是 LLM 收到
        #     [來源4] MIL-STD-810H (第 276 頁)
        #     （空白）
        # 那一頁正是含「1.1 ±0.3 g/m3」的段落 —— 檢索撈到了、也進了 keep_ids，
        # 卻拿到 0 個字元。空塊仍保留在 sources（使用者點得到），只是不進脈絡。
        if not text:
            continue
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

    vector_query, spec_doc_id = retrieval.resolve_spec_scope(db, search_query)
    embeddings = ai.embed_query(vector_query)
    if not embeddings:
        def _embed_error():
            yield f"data: {json.dumps({'type': 'error', 'message': '嵌入計算失敗'}, ensure_ascii=False)}\n\n"
        return StreamingResponse(_embed_error(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    top_k = payload.top_k or 5
    filtered = _hybrid_filtered(
        db, payload, search_query, embeddings[0], vector_config, top_k,
        document_id=spec_doc_id,
    )
    filtered = _augment_for_values(
        db, payload, lambda q: (ai.embed_query(q) or [None])[0],
        vector_config, top_k, question, filtered,
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
        if not text:      # 同上：空脈絡不送進 LLM
            continue
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

    user_id = current_user.id
    conversation_id = payload.conversation_id

    def generate():
        # 純 RAG 模式原本沒有後端存檔，靠前端「整包 PUT」持久化。多對話串下
        # 那等於用當前畫面覆蓋掉「最近更新的那一條」—— 切到別條對話再問一題
        # 就會把它洗掉。三種模式的存檔方式必須一致，才不會有一條路徑漏掉。
        answer_parts = []
        try:
            if not contexts:
                msg = "查無足夠的相關內容，請提供更多文件或調整問題。"
                answer_parts.append(msg)
                yield f"data: {json.dumps({'type': 'content', 'text': msg}, ensure_ascii=False)}\n\n"
            else:
                for stream_chunk in ai.generate_rag_answer_stream(
                    final_question, contexts, conversation_history=history,
                    system_prompt=rag_prompts["system_prompt"],
                    user_template=rag_prompts["user_template"],
                ):
                    if stream_chunk.get("type") == "content":
                        answer_parts.append(stream_chunk.get("text") or "")
                    yield f"data: {json.dumps(stream_chunk, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'sources', 'sources': sources_data, 'is_followup': is_followup, 'optimized_query': optimized_query}, ensure_ascii=False)}\n\n"

            saved = conversations.append_message(
                db, user_id, conversation_id,
                question=question, answer="".join(answer_parts),
                sources=sources_data, mode="rag",
            )
            yield f"data: {json.dumps({'type': 'done', 'conversation_id': saved.id if saved else None}, ensure_ascii=False)}\n\n"
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


# ── 多對話串（V6）──────────────────────────────────────────
# 舊的 /conversation 三個端點保留，指向「最近更新的那一條」，
# 讓尚未改版的前端不會壞掉；新前端改用底下的 /conversations。


@router.post("/followup-check")
def followup_check(
    payload: dict,
    current_user=Depends(get_current_user),
):
    """判斷這句是延續還是新問題。純規則、不呼叫 LLM，供前端隨打字即時顯示。

    為什麼放在後端而不是前端自己判斷：規則本體（主體白名單、參數名、
    長度門檻、改寫方式）都在 services/agent.py。前端重寫一份必然會漂移，
    然後出現「標籤說會延續、實際檢索沒延續」這種最難查的落差。
    """
    question = (payload or {}).get("question") or ""
    history = (payload or {}).get("conversation_history") or []
    return agent.classify_followup(question, history)


@router.get("/conversations")
def list_conversations(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """側邊欄用的對話清單（不含 messages）。"""
    rows = conversations.list_for_user(db, current_user.id)
    return {"conversations": [conversations.to_summary(r) for r in rows]}


@router.post("/conversations")
def create_conversation(
    payload: dict | None = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """開一條新對話。前端按「＋ 新對話」時呼叫。"""
    title = (payload or {}).get("title")
    row = conversations.create(db, current_user.id, title=title)
    return conversations.to_summary(row)


@router.get("/conversations/{conversation_id}")
def get_conversation_by_id(
    conversation_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    row = conversations.get_owned(db, current_user.id, conversation_id)
    if not row:
        raise HTTPException(status_code=404, detail="找不到這條對話")
    return {**conversations.to_summary(row), "messages": row.messages or []}


@router.patch("/conversations/{conversation_id}")
def update_conversation(
    conversation_id: str,
    payload: dict,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """改標題或釘選狀態。"""
    row = conversations.get_owned(db, current_user.id, conversation_id)
    if not row:
        raise HTTPException(status_code=404, detail="找不到這條對話")
    if "title" in payload:
        title = (payload.get("title") or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="標題不可為空")
        row.title = title[:200]
    if "is_pinned" in payload:
        row.is_pinned = bool(payload.get("is_pinned"))
    db.commit()
    db.refresh(row)
    return conversations.to_summary(row)


@router.delete("/conversations/{conversation_id}", status_code=204)
def delete_conversation(
    conversation_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    row = conversations.get_owned(db, current_user.id, conversation_id)
    if not row:
        raise HTTPException(status_code=404, detail="找不到這條對話")
    db.delete(row)
    db.commit()
    return None


@router.get("/conversation")
def get_conversation(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """相容用：取最近更新的那條對話。新前端請改用 /conversations。"""
    rows = conversations.list_for_user(db, current_user.id)
    return {"messages": (rows[0].messages if rows else []) or []}


@router.put("/conversation")
def save_conversation(
    payload: dict,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """相容用：整包覆寫最近更新的那條對話（沒有就開一條）。

    V6 之後應改用 /conversations/{id}；這裡保留是為了讓尚未改版的前端
    不會靜默寫進舊表 —— 那會造成「前端顯示有、側邊欄看不到」的分裂狀態。
    """
    messages = payload.get("messages", [])
    rows = conversations.list_for_user(db, current_user.id)
    if rows:
        row = rows[0]
        row.messages = messages
        flag_modified(row, "messages")
        db.commit()
    else:
        first_q = (messages[0].get("question") if messages else "") or ""
        conversations.create(db, current_user.id,
                             title=conversations.make_title(first_q),
                             messages=messages)
    return {"ok": True}


@router.delete("/conversation", status_code=204)
def clear_conversation(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """相容用：清除這位使用者的全部對話串。

    V6 的「清除歷史」語意變了 —— 舊版只有一條，清掉就是清掉；
    現在有多條，所以這個端點會刪光全部。前端改版後應改用
    DELETE /conversations/{id} 逐條刪除。
    """
    db.query(models.Conversation).filter(
        models.Conversation.user_id == current_user.id
    ).delete()
    db.commit()
    return None

