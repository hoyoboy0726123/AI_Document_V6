"""撈寬再精選（rerank）：對檢索候選打分重排，把「真正含規格/判定/程序」的乾淨段落
排到前面、把修訂紀錄/續頁/目錄等垃圾踢掉，再餵生成。

兩種後端（RAG_RERANK_BACKEND）：
- cross_encoder：專門的 cross-encoder 模型（CPU），~1-3s，業界標準、品質佳，建議。
- llm：用既有生成模型（gemma）打分，零安裝但很慢（本機 ~100s）。

任何失敗（模型載入 / 呼叫 / 解析）都退回原順序，絕不讓檢索整個壞掉。
cross_encoder 載入失敗會自動降級到 llm。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from typing import List, Optional, Tuple

from ..core.config import settings
from . import ai

logger = logging.getLogger(__name__)


def _resolve_device() -> str:
    """decide the cross-encoder device.

    原本寫死 "cpu"，於是 24GB 的顯卡在 rerank 期間全程閒置，而 rerank 是每次
    查詢都會跑的階段。設定值 auto 時偵測 CUDA，偵測失敗一律退回 cpu
    （載入失敗還有既有的 _CE_FAILED 降級路徑接住）。
    """
    want = str(getattr(settings, "RAG_RERANK_DEVICE", "auto") or "auto").lower()
    if want != "auto":
        return want
    try:
        import torch  # noqa: PLC0415
        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:  # noqa: BLE001
        return "cpu"

# 送進 cross-encoder 的每塊字元數。
#
# 原本 500，但實測語料的平均塊長是 1465 字元 —— 也就是 reranker 對每塊的後
# 三分之二完全沒看到就在打分，而規範文件的數值與表格列常在塊的後半。
# max_length=512 是 token 上限，英文約當 2000 字元，所以瓶頸一直是這個常數
# 而不是模型。取 1800 與切塊的 max_chars 一致。
_SNIPPET_CHARS = getattr(settings, "RAG_RERANK_SNIPPET_CHARS", 1800)

# ── cross-encoder 後端 ───────────────────────────────────────────────
_CE_MODEL = None
_CE_LOCK = threading.Lock()
_CE_FAILED = False  # 載入失敗就不再重試，直接走 llm


def _get_cross_encoder():
    """延遲載入 cross-encoder（單例，CPU）。失敗回 None 並標記降級。"""
    global _CE_MODEL, _CE_FAILED
    if _CE_MODEL is not None:
        return _CE_MODEL
    if _CE_FAILED:
        return None
    with _CE_LOCK:
        if _CE_MODEL is not None:
            return _CE_MODEL
        if _CE_FAILED:
            return None
        try:
            import os

            from sentence_transformers import CrossEncoder

            model_name = getattr(settings, "RAG_RERANK_MODEL", "BAAI/bge-reranker-base")
            _device = _resolve_device()
            logger.info("rerank: loading cross-encoder %s (%s) ...", model_name, _device)
            # 離線優先：已快取就免連網（秒載、無網路也可用，利於打包 .exe）；
            # 沒快取再允許下載。
            #
            # 原本是在函式內設 os.environ["HF_HUB_OFFLINE"]，但那完全無效 ——
            # huggingface_hub 在 **import 時** 就把它讀成模組常數：
            #     import 時 HF_HUB_OFFLINE 常數 = False
            #     執行期設環境變數後        = False   ← 沒變
            # 於是每次冷啟都會實際連 HuggingFace（審查實測約 15 次 HTTP 請求、
            # 首次檢索 45.88 秒，暖機後 0.42 秒），對打包成單檔 .exe 離線部署
            # 是實際風險。
            # CrossEncoder 本身支援 local_files_only 參數，用它才真的有效。
            try:
                _CE_MODEL = CrossEncoder(model_name, device=_device, max_length=512,
                                         local_files_only=True)
            except Exception as offline_exc:  # noqa: BLE001
                logger.info("rerank: 本地無快取（%s），改為允許下載", type(offline_exc).__name__)
                _CE_MODEL = CrossEncoder(model_name, device=_device, max_length=512)
            logger.info("rerank: cross-encoder loaded")
            return _CE_MODEL
        except Exception as e:
            _CE_FAILED = True
            logger.warning("rerank: cross-encoder load failed, fallback to llm: %s", e)
            return None


def _score_cross_encoder(query: str, candidates) -> Optional[List[float]]:
    model = _get_cross_encoder()
    if model is None:
        return None
    try:
        pairs = [
            (query, (getattr(c, "text", "") or "")[:_SNIPPET_CHARS])
            for c, _ in candidates
        ]
        scores = model.predict(pairs)
        return [float(s) for s in scores]
    except Exception as e:
        logger.warning("rerank: cross-encoder predict failed: %s", e)
        return None


# ── llm 後端（gemma 打分）────────────────────────────────────────────
_RERANK_SYSTEM = (
    "You are a passage relevance rater for a document QA system. "
    "Output ONLY valid JSON, no prose, no markdown."
)

_RERANK_TEMPLATE = """\
針對使用者問題，為每個候選段落的「相關性」打 0-10 分。

評分標準：
- 8-10：段落直接包含問題主題的規格 / 判定標準 / 測試程序 / 數值等實質內容。
- 4-7：段落屬於該主題但只是片段、續頁或摘要。
- 0-3：與問題主題無關，或屬於修訂紀錄、目錄、版權頁、無意義表格殘片等。

使用者問題：
{question}

候選段落：
{passages}

只輸出 JSON，格式為 {{"1": 分數, "2": 分數, ...}}，鍵為段落編號、值為 0-10 整數。"""


def _parse_scores(raw: str) -> dict:
    if not raw:
        return {}
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    candidate = m.group(0) if m else raw
    try:
        data = json.loads(candidate)
        out = {}
        for k, v in data.items():
            try:
                out[int(str(k).strip())] = float(v)
            except (ValueError, TypeError):
                continue
        return out
    except Exception:
        out = {}
        for km, vm in re.findall(r'"?(\d+)"?\s*:\s*(\d+(?:\.\d+)?)', candidate):
            out[int(km)] = float(vm)
        return out


def _score_llm(query: str, candidates) -> Optional[dict]:
    passages = []
    for i, (chunk, _) in enumerate(candidates, start=1):
        text = (getattr(chunk, "text", "") or "")[:_SNIPPET_CHARS].replace("\n", " ").strip()
        page = getattr(chunk, "page", None)
        passages.append(f"[{i}] (第{page}頁) {text}")
    prompt = _RERANK_TEMPLATE.format(question=query, passages="\n\n".join(passages))
    messages = [
        {"role": "system", "content": _RERANK_SYSTEM},
        {"role": "user", "content": prompt},
    ]
    try:
        raw = ai._chat_with_ollama(messages, response_format="json", think=False)
    except Exception as e:
        logger.warning("rerank llm call failed: %s", e)
        return None
    return _parse_scores(raw)


def _guarantee_fused_top(candidates, result, top_n):
    # 可關閉：實測 rerank 的 MRR 比不 rerank 還低，懷疑是這道保底把
    # cross-encoder 判為不相關的塊硬塞到第 1 名所致（審查實測有一例
    # CE 分數 0.172 vs 最佳 0.998 被放到位置 0，連帶污染 primary_page 的頁距視窗）。
    if not getattr(settings, "RAG_RERANK_GUARANTEE_TOP", True):
        return result[:top_n]
    """保底：融合(向量+BM25)排第 1 的候選必須留在最終結果裡。

    rerank 對「數字表 / 跨語言 / 精準關鍵字命中」評分常不準，可能把融合第 1 名（例如
    唯一含查詢數值 "101.78" 的那一列）整個踢出 top_n。融合第 1 名是最強檢索信號，
    保底把它放回（置頂），避免「明明撈到了卻被 rerank 丟掉」。
    """
    res = result[:top_n]
    if not candidates:
        return res
    top1 = candidates[0]
    if any(r[0] is top1[0] for r in res):
        return res
    return ([top1] + res)[:top_n]


# ── 對外入口 ─────────────────────────────────────────────────────────
def rerank(query: str, candidates: List[Tuple[object, float]], top_n: int):
    """對 candidates=[(chunk, score)] 重排，回傳重排後的 [(chunk, score)]（最多 top_n）。

    失敗時回傳原順序的前 top_n。
    """
    if not candidates:
        return []
    if not getattr(settings, "RAG_RERANK", True) or len(candidates) <= 1:
        return candidates[:top_n]

    backend = getattr(settings, "RAG_RERANK_BACKEND", "cross_encoder")

    # 1) cross-encoder（優先）
    if backend == "cross_encoder":
        scores = _score_cross_encoder(query, candidates)
        if scores is not None:
            min_score = getattr(settings, "RAG_RERANK_CE_MIN_SCORE", 0.0)
            ranked = [
                (c, orig, scores[i], i)
                for i, (c, orig) in enumerate(candidates)
            ]
            kept = [r for r in ranked if r[2] >= min_score] or ranked
            kept.sort(key=lambda r: (-r[2], r[3]))
            logger.info(
                "rerank[cross_encoder]: %d -> %d kept (top_n=%d)",
                len(candidates), len(kept), top_n,
            )
            return _guarantee_fused_top(candidates, [(c, orig) for c, orig, _s, _i in kept], top_n)
        # cross-encoder 不可用 → 降級到 llm

    # 2) llm 後端
    scores_map = _score_llm(query, candidates)
    if not scores_map:
        logger.warning("rerank: no scores, keeping original order")
        return candidates[:top_n]

    min_score = getattr(settings, "RAG_RERANK_MIN_SCORE", 3)
    ranked = []
    for i, (chunk, orig_score) in enumerate(candidates, start=1):
        s = scores_map.get(i, float(min_score))
        ranked.append((chunk, orig_score, s, i))
    kept = [r for r in ranked if r[2] >= min_score] or ranked
    kept.sort(key=lambda r: (-r[2], r[3]))
    logger.info("rerank[llm]: %d -> %d kept (top_n=%d)", len(candidates), len(kept), top_n)
    return _guarantee_fused_top(candidates, [(chunk, orig_score) for chunk, orig_score, _s, _i in kept], top_n)


def top_relevance(query: str, chunks: List[object]) -> Optional[float]:
    """回傳這批 chunks 對 query 的「最高 cross-encoder 相關性分數」（0-1）當信心訊號。

    供低信心 fallback 判斷用：實測相關題 top ≥0.4、離題 ≈0.00。
    cross-encoder 不可用時回 None（呼叫端應略過 gate，不要因此退化）。
    """
    if not chunks:
        return None
    model = _get_cross_encoder()
    if model is None:
        return None
    try:
        pairs = [(query, (getattr(c, "text", "") or "")[:_SNIPPET_CHARS]) for c in chunks]
        scores = model.predict(pairs)
        return float(max(scores)) if len(scores) else None
    except Exception as e:
        logger.warning("top_relevance scoring failed: %s", e)
        return None
