import base64
import io
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image

from ..core.config import settings
from .ollama_client import get_client

logger = logging.getLogger(__name__)


# 英文語料實測 4.52 字元/token（Ollama prompt_eval_count 量測）。取 4.0 保守，
# 因為中文問題與中文答案的比率接近 1，混合後實際值會低於純英文。
_CHARS_PER_TOKEN = 4.0


def effective_rag_budget() -> int:
    """依實際 OLLAMA_NUM_CTX 動態夾限 RAG context 字數預算。

    input+output 共用同一 context window；此函式回傳「可給 context 的字數上限」，
    並保留額度給生成，避免 prompt 把視窗塞滿導致模型吐空。

    原本的註解寫「本語料 CJK 為主，約 1 token/字」，於是直接拿 num_ctx 當字數
    上限。但語料其實是英文 MIL-STD 規範，實測 **4.52 字元/token**（用 Ollama 的
    prompt_eval_count 量的）。結果是 5192 字元的脈絡只換到 1151 tokens，
    8192 的視窗有 86% 從未被使用 —— 卻同時因為「預算用完」而把後面撈到的
    段落截成空字串。使用者看到的就是「答案裡沒有數值，但來源清單裡明明有」。

    改成以 token 為單位換算，並保留 prompt 本身（system prompt、模板、問題、
    對話歷史）的額外開銷。
    """
    nctx = getattr(settings, "OLLAMA_NUM_CTX", None) or 4096
    reserve_tokens = getattr(settings, "RAG_OUTPUT_RESERVE_TOKENS", 2048)
    # system prompt + user 模板 + 問題 + 對話歷史。這些原本完全沒被計入。
    overhead_tokens = getattr(settings, "RAG_PROMPT_OVERHEAD_TOKENS", 1500)
    avail_tokens = max(512, nctx - reserve_tokens - overhead_tokens)
    cap = getattr(settings, "RAG_CONTEXT_BUDGET_CHARS", 20000)
    return max(1500, min(cap, int(avail_tokens * _CHARS_PER_TOKEN)))


DOCUMENT_SUGGESTION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "classification": {"type": "string"},
        "classification_is_new": {"type": "boolean"},
        "classification_reason": {"type": "string"},
        "project": {"type": "string"},
        "project_is_new": {"type": "boolean"},
        "project_reason": {"type": "string"},
        "keywords": {
            "type": "array",
            "items": {"type": "string"},
        },
        "metadata": {
            "type": "object",
            "additionalProperties": True,
        },
    },
    "required": [
        "summary",
        "classification",
        "classification_is_new",
        "keywords",
        "metadata",
    ],
}

FOLLOWUP_INTENT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_followup": {"type": "boolean"},
        "needs_new_search": {"type": "boolean"},
        "optimized_query": {"type": "string"},
        "search_keywords": {"type": "string"},
        "reasoning": {"type": "string"},
    },
    "required": [
        "is_followup",
        "needs_new_search",
        "optimized_query",
        "search_keywords",
    ],
}

SUGGESTED_QUESTION_SCHEMA: Dict[str, Any] = {
    "type": "array",
    "items": {"type": "string"},
    "minItems": 1,
    "maxItems": 5,
}


def _safe_json_loads(payload: str) -> Dict[str, Any]:
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        logger.warning("LLM 回傳的 JSON 解析失敗：%s", payload)
        return {}


def _chat_with_ollama(
    messages: List[Dict[str, Any]],
    *,
    model: Optional[str] = None,
    response_format: Optional[Any] = None,
    think: bool = False,
    options: Optional[Dict[str, Any]] = None,
) -> str:
    import time
    client = get_client()
    t0 = time.time()
    result = client.chat(
        messages,
        model=model or settings.OLLAMA_LLM_MODEL,
        format=response_format,
        think=think,
        options=options,
    )
    elapsed = time.time() - t0
    logger.info("_chat_with_ollama elapsed=%.1fs think=%s output_len=%d preview=%s",
                elapsed, think, len(result), repr(result[:120]))
    return result


def _chat_with_provider(
    messages: List[Dict[str, Any]],
    *,
    model: Optional[str] = None,
    response_format: Optional[Any] = None,
    think: bool = False,
) -> str:
    """Provider-aware chat: honour the configured LLM_PROVIDER.

    Ollama keeps its existing path verbatim (including the `think` flag, which
    is Ollama-specific). Any cloud provider (e.g. Gemini) goes through the
    provider abstraction so switching providers in admin/.env actually routes
    RAG synthesis there instead of always hitting Ollama.
    """
    from .llm_provider import get_llm_provider

    provider = get_llm_provider()
    if getattr(provider, "name", "ollama") == "ollama":
        return _chat_with_ollama(
            messages, model=model, response_format=response_format, think=think
        )
    import time
    t0 = time.time()
    result = provider.chat(messages, model=model, format=response_format)
    logger.info("_chat_with_provider provider=%s elapsed=%.1fs output_len=%d preview=%s",
                provider.name, time.time() - t0, len(result), repr(result[:120]))
    return result


_CITATION_RE = re.compile(r"\[來源\s*\d+\]")


def _strip_citations(text: str) -> str:
    """把歷史答案裡的 [來源N] 換成「(前一輪)」。

    歷史是以純文字塞在脈絡旁邊的。實測模型會整句照抄前一輪的內容，連 [來源1]
    一起抄過來 —— 但本輪的來源 1 是完全不同的段落。使用者回頭查證時翻不到那個
    數值，看起來就像系統在幻覺（實測：「標準大氣條件 23 ± 2 °C、50 ± 5% RH」
    掛在一個不含這些值的來源上）。

    這裡不試圖阻止照抄（提示詞約束實測不可靠），而是讓照抄的結果至少是誠實的：
    抄過去的是「(前一輪)」而不是一個假的來源編號。
    """
    return _CITATION_RE.sub("（前一輪）", text or "")


def _prepare_history(conversation_history: Optional[List[Dict[str, str]]]) -> List[Dict[str, str]]:
    # Local light-weight sanitizer to avoid leaking control tokens into prompts
    import re as _re

    def _scrub(text: str) -> str:
        if not text:
            return text
        cleaned = _re.sub(r"[\u200B\u200C\u200D\u2060\ufeff]", "", text)
        for t in (r"<\|im_start\|>", r"<\|im_end\|>", r"<\|assistant\|>", r"<\|user\|>", r"<s>", r"</s>", r"<<SYS>>", r"</SYS>"):
            cleaned = _re.sub(t, "", cleaned, flags=_re.IGNORECASE)
        cleaned = _re.sub(r"^(\s*)(assistant|user|system)\s*:\s*", r"\1", cleaned, flags=_re.IGNORECASE | _re.MULTILINE)
        return cleaned.strip()

    history: List[Dict[str, str]] = []
    if not conversation_history:
        return history

    for turn in conversation_history[-5:]:
        question = _scrub(turn.get("question") or "")
        answer = _scrub(turn.get("answer") or "")
        if question:
            history.append({"role": "user", "content": question})
        if answer:
            history.append({"role": "assistant", "content": answer})
    return history


def generate_document_suggestion(
    *,
    text: str,
    classifications: List[str],
    projects: List[str],
    segments: Optional[List[Dict[str, Any]]] = None,
    max_text_chars: int = 8000,
) -> Dict[str, Any]:
    truncated_text = (text or "").strip()[:max_text_chars]

    segments_summary = []
    if segments:
        for segment in segments[:20]:
            snippet = str(segment.get("text", "")).strip().replace("\n", " ")
            if len(snippet) > 200:
                snippet = f"{snippet[:200]}..."
            page = segment.get("page")
            paragraph = segment.get("paragraph_index")
            segments_summary.append(f"- Page {page} / Paragraph {paragraph}: {snippet}")

    prompt = f"""
You are an assistant that extracts document metadata, summary, keywords, and suggested classification/project values.
IMPORTANT: The summary MUST be in Traditional Chinese (Taiwan). Keywords should be in Traditional Chinese or English as appropriate.

Document snippet:
```
{truncated_text}
```

Available classifications:
{chr(10).join(f"- {item}" for item in classifications) if classifications else "- (none listed)"}

Available projects:
{chr(10).join(f"- {item}" for item in projects) if projects else "- (none listed)"}

Important segments:
{chr(10).join(segments_summary) if segments_summary else "-"}

Return a JSON object strictly following the provided schema.
"""

    raw_response = _chat_with_provider(
        [{"role": "user", "content": prompt}],
        response_format=DOCUMENT_SUGGESTION_SCHEMA,
    )
    payload = _safe_json_loads(raw_response)

    return {
        "summary": payload.get("summary", ""),
        "classification": payload.get("classification"),
        "classification_is_new": bool(payload.get("classification_is_new")),
        "classification_reason": payload.get("classification_reason"),
        "project": payload.get("project"),
        "project_is_new": bool(payload.get("project_is_new")),
        "project_reason": payload.get("project_reason"),
        "keywords": payload.get("keywords") or [],
        "metadata": payload.get("metadata") or {},
    }


_PAGE_GAP_THRESHOLD = 5  # 頁碼差超過此值視為不同章節


def _page_label(block: Dict, idx: int) -> str:
    page = block.get("page") or "?"
    gap = block.get("page_gap")
    if idx == 0 or gap is None:
        return f"第 {page} 頁"
    if gap <= _PAGE_GAP_THRESHOLD:
        return f"第 {page} 頁（與主要來源相鄰，頁距 {gap}）"
    return f"第 {page} 頁 ⚠️ 頁距 {gap}，可能為不同章節"


def _split_md_row(line: str) -> List[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _augment_tables_rowwise(text: str) -> str:
    """偵測 markdown 管線表格，於該表後附「逐列 key=value」序列化。

    動機：小模型（如 gemma-12b）讀寬 pipe-table 做「查某列某欄的值」會失敗（對不齊 row-key↔欄），
    但逐列 `欄=值` 格式就讀得出來（已實測）。原表格保留（供整表呈現），額外附逐列（供查特定值）。
    """
    if not text or "|" not in text:
        return text
    lines = text.split("\n")
    out: List[str] = []
    i, n = 0, len(lines)
    while i < n:
        is_table_head = (
            lines[i].count("|") >= 2
            and i + 1 < n
            and "-" in lines[i + 1]
            and set(lines[i + 1].strip()) <= set("|-: ")
            and lines[i + 1].count("|") >= 2
        )
        if is_table_head:
            header = _split_md_row(lines[i])
            j = i + 2
            data = []
            while j < n and lines[j].count("|") >= 2:
                data.append(_split_md_row(lines[j]))
                j += 1
            out.append("\n".join(lines[i:j]))  # 原表格保留
            rw = []
            for k, row in enumerate(data, start=1):
                pairs = [f"{h}={v}" for h, v in zip(header, row) if h.strip() and v.strip()]
                if pairs:
                    rw.append(f"第{k}列: " + "; ".join(pairs))
            if rw:
                out.append("（上表逐列）：\n" + "\n".join(rw))
            i = j
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


def _build_rag_messages(
    question: str,
    context_blocks: List[Dict[str, str]],
    conversation_history: Optional[List[Dict[str, str]]],
    system_prompt: Optional[str],
    user_template: Optional[str],
) -> List[Dict[str, str]]:
    """組裝傳給 LLM 的 messages，支援自訂 system_prompt 與 user_template。

    user_template 可包含三個佔位符：
      {{question}}  → 使用者問題
      {{context}}   → 向量段落文字
      {{history}}   → 對話歷史區塊（空字串或「對話歷史...」整段）
    """
    from .system_config import DEFAULT_RAG_SYSTEM_PROMPT, DEFAULT_RAG_USER_TEMPLATE

    sys_msg = system_prompt if system_prompt is not None else DEFAULT_RAG_SYSTEM_PROMPT
    tmpl = user_template if user_template is not None else DEFAULT_RAG_USER_TEMPLATE

    # 只對「最相關的前 N 個來源」做表格逐列增強：逐列序列化會讓含表格的塊大約加倍，
    # 若每個來源都加會撐爆 num_ctx 導致生成退化（實測）。最相關的表格通常排在最前面。
    _aug_top = getattr(settings, "RAG_TABLE_ROWWISE_TOP", 1)
    context_text = "\n\n".join(
        f"[來源{block.get('source_num', idx + 1)}] {block.get('title') or '未命名段落'} ({_page_label(block, idx)})\n"
        + (_augment_tables_rowwise((block.get('text') or '').strip()) if idx < _aug_top else (block.get('text') or '').strip())
        for idx, block in enumerate(context_blocks)
    )

    history_text = ""
    if conversation_history:
        recent = conversation_history[-2:]
        history_text = "\n".join(
            f"Q: {turn.get('question', '')}\nA: {_strip_citations(turn.get('answer', ''))}"
            for turn in recent
        )
    history_section = (
        "對話歷史（最近 2 輪，僅供理解語境）：\n"
        f"{history_text}\n"
        "注意：以上歷史「不是」本次的參考來源。若要沿用其中的數值，必須在下方"
        "可用段落中重新找到依據；找不到就標記為（前一輪，本次來源未涵蓋），"
        "不可為它掛上 [來源N]。\n"
    ) if history_text else ""

    prompt = (
        tmpl
        .replace("{{question}}", question)
        .replace("{{context}}", context_text)
        .replace("{{history}}", history_section)
    )

    return [
        {"role": "system", "content": sys_msg},
        {"role": "user", "content": prompt},
    ]


def generate_rag_answer(
    question: str,
    context_blocks: List[Dict[str, str]],
    conversation_history: Optional[List[Dict[str, str]]] = None,
    system_prompt: Optional[str] = None,
    user_template: Optional[str] = None,
) -> str:
    if not question:
        raise ValueError('問題不可為空白')
    if not context_blocks:
        return '查無足夠的相關內容，請提供更多文件或調整問題。'

    messages = _build_rag_messages(question, context_blocks, conversation_history, system_prompt, user_template)
    # think=False: thinking models (gemma4) otherwise spend 60-120s reasoning per answer
    # and blow past OLLAMA_TIMEOUT. Disabling thinking is ~10x faster with equal/better answers.
    return _chat_with_provider(messages, think=False)


_LOWCONF_TEMPLATE_TIPS = (
    "建議調整查詢：\n"
    "- 改用文件中的實際術語或數值（例如型號、規格編號、頻率值）\n"
    "- 指定文件或分類以縮小範圍\n"
    "- 換更具體的關鍵字，或把問題拆得更小\n"
    "- 確認該資訊是否已上傳到系統"
)


def low_confidence_answer(
    question: str,
    context_blocks: List[Dict[str, Any]],
) -> str:
    """檢索信心偏低時的回應：不硬答，而是列出「最接近的內容」＋ LLM 動態產生的查詢修正建議。

    建議由 LLM 依「問題＋最接近片段」生成（走 _chat_with_provider，雲地皆可）；失敗退回模板。
    """
    closest_lines: List[str] = []
    for i, b in enumerate((context_blocks or [])[:3], start=1):
        title = b.get("title") or "未命名段落"
        page = b.get("page")
        snip = re.sub(r"\s+", " ", (b.get("text") or "").strip())[:160]
        loc = f" 第{page}頁" if page else ""
        closest_lines.append(f"- [{i}] {title}{loc}：{snip}…")
    closest = "\n".join(closest_lines) if closest_lines else "（資料庫中沒有可顯示的接近內容）"

    tips: Optional[str] = None
    if closest_lines:
        try:
            sys_msg = "你是文件問答助手。資料庫中沒有與使用者問題高度相關的內容。"
            prompt = (
                f"使用者問題：{question}\n\n"
                f"資料庫中最接近的內容片段：\n{closest}\n\n"
                "請用繁體中文簡短回覆：\n"
                "1) 用一句話說明資料庫裡比較多的是什麼主題（依上面片段判斷）。\n"
                "2) 給 2~3 條具體的查詢修正建議，幫使用者更可能找到想要的資訊"
                "（例如改用哪些術語／數值、指定哪份文件或分類、換什麼關鍵字）。\n"
                "不要杜撰片段中沒有的事實，也不要直接回答原問題本身。"
            )
            tips = _chat_with_provider(
                [{"role": "system", "content": sys_msg}, {"role": "user", "content": prompt}],
                think=False,
            )
        except Exception as e:
            logger.warning("low-confidence suggestion generation failed: %s", e)
    if not (tips and tips.strip()):
        tips = _LOWCONF_TEMPLATE_TIPS

    return (
        "⚠️ 資料庫中沒有找到與您問題「高度相關」的內容（檢索信心偏低）。"
        "以下是最接近的片段，但可能不直接相關：\n\n"
        f"{closest}\n\n"
        f"{tips.strip()}"
    )


def generate_rag_answer_stream(
    question: str,
    context_blocks: List[Dict[str, str]],
    conversation_history: Optional[List[Dict[str, str]]] = None,
    system_prompt: Optional[str] = None,
    user_template: Optional[str] = None,
):
    """以串流方式 yield chunks: {"type": "thinking"|"content", "text": "..."}"""
    if not context_blocks:
        yield {"type": "content", "text": "查無足夠的相關內容，請提供更多文件或調整問題。"}
        return

    messages = _build_rag_messages(question, context_blocks, conversation_history, system_prompt, user_template)
    from .llm_provider import get_llm_provider
    provider = get_llm_provider()
    if getattr(provider, "name", "ollama") == "ollama":
        client = get_client()
        for chunk in client.chat_stream(messages, model=settings.OLLAMA_LLM_MODEL, think=False):
            yield chunk
    else:
        for chunk in provider.chat_stream(messages):
            yield chunk


_EMBED_MAX_CHARS = 7000  # qwen3-embedding:8b supports 8192 tokens (~7000 English chars)
_EMBED_BATCH_SIZE = 10   # process N chunks per request to avoid timeout

# Qwen3-Embedding 是非對稱模型：查詢端要加 instruct 前綴、文件端不加。
# 原本查詢與文件共用同一條路徑（embed_texts），完全沒有非對稱處理。
# 官方說明不加約掉 1-5% 檢索表現，跨語言（中文問英文語料）情境收益偏高端，
# 因為 instruct 能明示「跨語言檢索」這個意圖。
#
# 只動查詢端 → 文件向量不變 → **不需要重建 FAISS 索引**，改壞了改回來即可。
# 前綴刻意保持短。實測（golden set 30 題，評測為確定性、同設定兩次結果相同）：
#
#   無前綴                      recall@20 0.867  hit@5 0.700  MRR 0.506
#   長前綴（130 字元，官方風格） recall@20 0.867  hit@5 0.667  MRR 0.462   ← 淨賠
#   短前綴（64 字元，現行）      recall@20 0.900  hit@5 0.700  MRR 0.447
#
# 官方文件說「不加掉 1-5%」，但在本語料上長前綴反而讓排序變差 —— 查詢只有
# 10-20 字元，130 字元的前綴會稀釋查詢本身。
#
# 短前綴的判讀要誠實：多 1 題進候選池，但 hit@5 完全沒動，也就是那 1 題沒有
# 轉化成使用者看得到的改善；MRR 下降則沒有實際後果，因為前 5 名全部都會進
# LLM 脈絡（RAG_MAX_CONTEXT_CHUNKS=6），排名先後不影響模型看到什麼。
# 結論是「功能中性」。保留的理由是召回為唯一無法事後補救的階段，多留餘裕；
# 等 reranker 升級（bge-reranker-v2-m3）之後，那 1 題才可能轉化。
# 要回退設 EMBEDDING_QUERY_INSTRUCT=False 即可，不需重建索引。
_QUERY_INSTRUCT = "Instruct: Retrieve relevant technical standard passages.\nQuery: "


def embed_query(text: str) -> List[List[float]]:
    """查詢用的嵌入（加 instruct 前綴）。

    與 embed_texts 分開，是為了讓「查詢」與「文件」兩端的差異顯性化 ——
    共用同一個函式時，非對稱處理沒有地方可放，也沒有人會發現它缺了。
    """
    if not (text or "").strip():
        return []
    if not getattr(settings, "EMBEDDING_QUERY_INSTRUCT", True):
        return embed_texts([text])
    return embed_texts([_QUERY_INSTRUCT + text])


def embed_texts(texts: List[str]) -> List[List[float]]:
    """經 embedding provider 抽象產生向量，受 EMBEDDING_PROVIDER 設定支配。

    預設 ollama（行為與舊版一致，用 OLLAMA_EMBED_MODEL）。注意：切換 embedding 後端
    （例如 gemini）會改變向量維度，與既有 FAISS 索引不相容，需重新向量化所有文件。
    """
    if not texts:
        return []

    from .llm_provider import get_embedding_provider

    provider = get_embedding_provider()
    truncated = [t[:_EMBED_MAX_CHARS] for t in texts]

    all_embeddings: List[List[float]] = []
    for i in range(0, len(truncated), _EMBED_BATCH_SIZE):
        batch = truncated[i:i + _EMBED_BATCH_SIZE]
        all_embeddings.extend(provider.embed(batch))

    if len(all_embeddings) != len(texts):
        raise RuntimeError("embedding 數量與輸入不符")
    return all_embeddings


def analyze_followup_intent(
    current_question: str,
    conversation_history: List[Dict[str, str]],
) -> Dict[str, object]:
    if not current_question:
        return {
            "is_followup": False,
            "needs_new_search": True,
            "optimized_query": "",
            "search_keywords": "",
            "reasoning": "",
        }

    history_text = "\n".join(
        f"Q: {turn.get('question')}\nA: {turn.get('answer')}\n"
        for turn in conversation_history[-3:]
    )

    prompt = f"""
You are an assistant that decides whether the latest question is a follow-up and produces ONE optimized search query.

Conversation history (most recent last):
{history_text or '(no history)'}

Current user question: {current_question}

Instructions for the optimized query:
- STEP 1 (decide first): Is the current question SELF-CONTAINED? It is self-contained when it
  already names its own subject/topic and would be fully understandable with no history at all.
  A DIFFERENT topic from the previous question is still self-contained — users change topics
  mid-conversation all the time.
  → If SELF-CONTAINED: return the current question UNCHANGED as the optimized query.
    Do NOT prepend, append, or merge anything from the previous question. Never combine
    two different topics into one query.
- STEP 2 (only if NOT self-contained): the question is elliptical — it leans on the previous
  question via pronouns (那/這/它/it/that), a bare parameter ("limit", "那濕度呢"), or would be
  meaningless alone. Then the subject from the PREVIOUS question MUST appear as the first
  term, followed by the new intent keywords. Never drop the inherited subject in this case.
- RULE 3: Return ONE phrase (<= 12 words), no commas or lists, spaces allowed only.
- RULE 4: Language must match the user's current question language.

Examples:
- Prev: "buying mode有幾種"  New: "料件有哪些種類"
  → self-contained (own subject 料件; topic change is allowed)
  Optimized: "料件有哪些種類"   ← UNCHANGED. "buying mode 料件種類" would be WRONG.
- Prev: "鹽霧測試的箱體溫度要維持在幾度"  New: "冷凝測試方法與條件"
  → self-contained (own subject 冷凝測試)
  Optimized: "冷凝測試方法與條件"   ← UNCHANGED. Do not prepend 鹽霧測試.
- Prev: "請問 Pressure test 有幾種測試?"  New: "我要知道測試力量"
  → elliptical (no subject of its own)
  Optimized: "Pressure test 測試力量 數值"
- Prev: "What are ESD test steps?"  New: "limit"
  → elliptical
  Optimized: "ESD test limit values"
- Prev: "shock test 測試標準"  New: "那關機測試標準呢"
  → elliptical (那…呢 refers back)
  BAD:  "關機測試 標準"          ← dropped "shock test", WRONG
  GOOD: "shock test 關機條件 標準" ← kept subject, CORRECT
- Prev: "vibration test procedure"  New: "pass criteria?"
  Optimized: "vibration test pass criteria"

Return JSON strictly following the schema.
"""

    raw = _chat_with_provider(
        [{"role": "user", "content": prompt}],
        response_format=FOLLOWUP_INTENT_SCHEMA,
    )
    payload = _safe_json_loads(raw)
    return {
        "is_followup": bool(payload.get("is_followup")),
        "needs_new_search": bool(payload.get("needs_new_search", True)),
        "optimized_query": payload.get("optimized_query", current_question),
        "search_keywords": payload.get("search_keywords", current_question),
        "reasoning": payload.get("reasoning", ""),
    }


def generate_suggested_questions(context: str, answered_question: str) -> List[str]:
    excerpt = context[:500]
    prompt = f"""
你是文件問答系統要提供延伸問題建議。請根據以下已回答的問題與摘要內容，再提供 3 個可追問的問題。

原始問題：{answered_question}
回答摘要：{excerpt}

請只回傳 JSON 陣列格式，如：
["問題 1","問題 2","問題 3"]
"""
    raw = _chat_with_provider(
        [{"role": "user", "content": prompt}],
        response_format=SUGGESTED_QUESTION_SCHEMA,
    )
    parsed = _safe_json_loads(raw)
    if isinstance(parsed, list):
        return [str(item) for item in parsed[:3]]
    return []


def generate_fallback_answer(question: str) -> str:
    prompt = f"""
你是一個沒有文件可查詢的助理，需要用常識回答使用者問題。
如果問題無法確定答案，請提出建議的查詢方向。

問題：{question}
"""
    return _chat_with_provider([
        {"role": "system", "content": "請以繁體中文（台灣）作答，嚴格禁止簡體中文。避免輸出控制標記或思考過程。"},
        {"role": "user", "content": prompt},
    ])


_FENCE_WRAPPER_RE = re.compile(r"^```[ \t]*(?:markdown|md)[ \t]*$", re.IGNORECASE)


def strip_markdown_fence_wrapper(text: str) -> str:
    """剝除 VL 模型「把整段輸出包進程式碼圍籬」的壞習慣。

    提示詞第 7 條已要求不要包圍籬，但模型不一定遵守（實測同一份 PDF 在不同機器上，
    有的會輸出 ```markdown ... ```）。這種包裝會造成兩個問題：
      1. 前端依 Markdown 規範把整段當程式碼區塊原樣顯示（表格、標題都不會渲染）。
      2. 圍籬符號混進 chunk 文字，污染全文檢索與 BM25 關鍵字比對。

    只剝「包裝用」的圍籬（整段包住、或標記為 markdown/md 的區塊），
    文件內真正的程式碼區塊（其他語言標記的 ```python 等）保留不動。
    """
    if not text or "```" not in text:
        return text
    stripped = text.strip()

    # 情況一：整段被「單一」圍籬包住（```markdown ... ``` 或裸 ``` ... ```）。
    # 必須確認剝完後內部不再有圍籬，否則代表是多區塊的情況，要交給下面處理，
    # 不能在這裡直接回傳（否則會殘留中間的圍籬符號）。
    whole = re.match(
        r"^```[ \t]*(?:markdown|md)?[ \t]*\n(.*?)\n?```$",
        stripped,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if whole and "```" not in whole.group(1):
        return whole.group(1).strip()

    # 情況二：模型分段輸出多個 ```markdown 區塊（實測一頁內會出現多組）
    if any(_FENCE_WRAPPER_RE.match(ln.strip()) for ln in stripped.split("\n")):
        kept: List[str] = []
        in_wrapper = False
        for line in stripped.split("\n"):
            bare = line.strip()
            if _FENCE_WRAPPER_RE.match(bare):
                in_wrapper = True          # 進入包裝區塊，丟掉這行開頭圍籬
                continue
            if in_wrapper and bare == "```":
                in_wrapper = False         # 對應的結尾圍籬也丟掉
                continue
            kept.append(line)
        return "\n".join(kept).strip()

    return stripped


# 我們自己塞進 prompt 的範例內容。模型會照抄，而抄進來的東西看起來就像文件內容 ——
# 這不是「猜哪些是雜訊」，是「把自己放進去的東西拿回來」，所以濾掉是安全的。
_PROMPT_ECHO_LINES = (
    re.compile(r"^\s*\|\s*項目\s*\|\s*條件A\s*\|\s*條件B\s*\|\s*$"),
    re.compile(r"^\s*\|\s*數值\s*\|\s*100\s*\|\s*200\s*\|\s*$"),
    re.compile(r"^\s*\|\s*<col\d>\s*\|.*$", re.I),
    re.compile(r"^\s*\|\s*<cell>\s*\|.*$", re.I),
    re.compile(r"^\s*Here is the (markdown|converted).*$", re.I),
)


def _strip_prompt_echo(text: str) -> str:
    """濾掉模型從 prompt 範例照抄過來的內容。

    只濾「我們確定自己放進 prompt 的字串」，不做任何雜訊猜測 ——
    後者已實測不可行：這份文件裡重複最多次的表格列是真的編碼原則表
    （4 次），比範例表（2 次）還頻繁，用頻率當判準會刪掉真內容。
    """
    if not text:
        return text
    lines = text.splitlines()
    kept = [ln for ln in lines if not any(p.match(ln) for p in _PROMPT_ECHO_LINES)]
    if len(kept) == len(lines):
        return text          # 沒有回聲就原字串回傳，不做任何其他處理
    out = "\n".join(kept)
    # 範例表被整張抄走時分隔列會留下孤兒。這條清理只在「確實移除了回聲」時才做 ——
    # 無條件套用會砍到跨 chunk 被切開的真表格（實測 MIL-STD-1366E 有 9 塊
    # 的分隔列剛好落在切點上，被誤判成孤兒）。
    out = re.sub(r"\n\|\s*(?:-{3,}\s*\|)+\s*\n(?!\s*\|)", "\n", out)
    return out


_CODE_TOKEN_RE = re.compile(r"\b(?=[A-Z0-9-]{4,}\b)(?=[A-Z0-9-]*[A-Z])(?=[A-Z0-9-]*\d)[A-Z0-9-]+\b")
# OCR/VL 最常見的形近混淆。只用這幾組是為了不把「真的不同的兩個料號」誤改成同一個。
_CONFUSABLE = str.maketrans({"O": "0", "I": "1", "L": "1", "S": "5", "B": "8", "Z": "2"})


def repair_code_tokens(vl_text: str, native_text: str) -> Tuple[str, int]:
    """用 PDF 原生文字層修正 VL 轉寫的形近字錯誤，回傳 (修正後文字, 修正數)。

    為什麼一定要修：實測這份教育訓練文件把料號 `22SW0` 轉寫成 `22SWO`
    （數字 0 → 字母 O）。文件內容主體就是料號與代號，使用者照著查會查不到，
    而答案看起來完全正常。軍規語料的數值有 _flag_unsourced_values 把關，
    代碼類字串一條防線都沒有。

    原生文字層是可信來源（PDF 自己的字元編碼），VL 只該用來補圖片與版面。
    因此只在「原生層有這個 token、VL 沒有、但 VL 有它的形近變體」時才改 ——
    不是猜測，是拿權威來源校正。
    """
    if not vl_text or not native_text:
        return vl_text, 0
    native_codes = set(_CODE_TOKEN_RE.findall(native_text.upper()))
    if not native_codes:
        return vl_text, 0
    vl_codes = set(_CODE_TOKEN_RE.findall(vl_text.upper()))
    # 以「去混淆後的形狀」建索引，找出 VL 版與原生版只差形近字的配對
    by_shape: Dict[str, str] = {}
    for c in native_codes:
        by_shape.setdefault(c.translate(_CONFUSABLE), c)
    fixed = 0
    for v in vl_codes - native_codes:
        target = by_shape.get(v.translate(_CONFUSABLE))
        if target and target != v:
            new = re.sub(r"\b" + re.escape(v) + r"\b", target, vl_text)
            if new != vl_text:
                vl_text = new
                fixed += 1
                logger.info("VL 形近字校正：%s → %s（依原生文字層）", v, target)
    return vl_text, fixed


def extract_text_with_vision(
    image_bytes_list: List[bytes],
    page_numbers: List[int],
    native_texts: Optional[Dict[int, str]] = None,
) -> List[Dict[str, Any]]:
    """Use VL model to extract text from PDF page images, preserving layout structure.

    Returns a list of segment dicts compatible with split_segments_into_chunks.

    native_texts：{頁碼: PDF 原生文字層}。有給的話會用它校正 VL 的形近字誤讀
    （實測料號 22SW0 被轉寫成 22SWO）。原生文字層是權威來源，VL 只補圖片與版面。
    """
    if not image_bytes_list:
        return []

    client = get_client()
    segments = []

    def _align14(img_bytes: bytes) -> bytes:
        """Resize image so both dimensions are multiples of 14 (qwen2.5vl patch size)."""
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        w, h = img.size
        new_w = max(14, ((w + 13) // 14) * 14)
        new_h = max(14, ((h + 13) // 14) * 14)
        if new_w != w or new_h != h:
            img = img.resize((new_w, new_h), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()

    prompt = (
        "You are a document digitization assistant. Convert this PDF page into clean GitHub-Flavored Markdown.\n\n"
        "STRICT OUTPUT RULES — output only Markdown, nothing else:\n\n"
        "1. HEADINGS — Use `##` for section titles, `###` for sub-sections. Preserve hierarchy as it appears on the page.\n\n"
        # 範例表格曾被模型整張照抄進輸出（實測 [Unit 0]基礎課程 有 3 個 chunk
        # 含「| 項目 | 條件A | 條件B | 100 | 200 |」，那不是投影片的內容，是這裡的範例）。
        # 中文欄名讓它看起來像真內容，混進中文語料就分不出來。改用一眼可辨的
        # 佔位符，並在輸出端把它濾掉（_strip_prompt_echo）——雙保險。
        "2. TABLES — MUST use Markdown pipe-table syntax INCLUDING the separator row.\n"
        "   Shape only (do NOT copy these placeholder words into your output):\n"
        "```\n"
        "| <col1> | <col2> | <col3> |\n"
        "| --- | --- | --- |\n"
        "| <cell> | <cell> | <cell> |\n"
        "```\n"
        "   Preserve column count exactly. Empty cells use a space. NEVER flatten tables into prose.\n"
        # 實測失效點：[Unit 0]基礎課程 的大類表被轉成 `| 01 CPU | CPU |` —— 模型把
        # 「代碼」與「名稱」兩個視覺欄位併進第一格，再把名稱複製到第二格。原本的
        # 「Preserve column count exactly」只約束欄「數」，沒約束欄「界」，所以擋不住。
        "   Each visual column is its own cell: NEVER merge two columns into one cell, and NEVER\n"
        "   duplicate one cell's content into another column. If a row's first column is a code and\n"
        "   the second is its name, they stay in separate cells.\n"
        "   Transcribe rows top-to-bottom in visual order. Do NOT add, drop, merge, or reorder rows.\n\n"
        # 這條是本次新增的核心防線。實測 qwen2.5vl 把 `09, 0A, 0B, 0C, 0K, 10` 這串
        # 代碼「整理」成連號，POWER MODULE 因此從 0A 變成 10（而 10 其實是 RESISTOR），
        # 0B/0C/0K 整列消失；同頁還把「僅秀出前200筆」寫成 100 筆、「料號」寫成「科號」。
        # 視覺模型的天性是把不整齊的序列順手補齊，必須明文禁止。
        "3. VERBATIM — Transcribe ONLY what is visibly printed. This overrides any instinct to tidy up:\n"
        "   - NEVER correct, complete, normalize, renumber, or reorder anything.\n"
        "   - Identifier sequences are NOT necessarily consecutive. `09, 0A, 0B, 0C, 0K, 10` is valid\n"
        "     as printed — do NOT renumber it into `09, 10, 11, 12`.\n"
        "   - Copy codes/IDs character by character. `0` (zero) and `O` (letter) are different; so are\n"
        "     `1`/`I`/`l` and `5`/`S` and `8`/`B`. If a code looks like a typo, copy the typo.\n"
        "   - Copy numbers exactly as printed. Never round, rescale, or change a digit.\n"
        "   - If text is cut off or unreadable, transcribe only the readable part. NEVER invent a row,\n"
        "     a list entry, or a table that is not visibly on the page.\n\n"
        # 這份語料裡有大量「投影片夾帶軟體操作截圖」的頁面。模型會把截圖裡的下拉選單
        # 誤當成文件表格並自行重組（實測第 7 頁：畫面上只有次分類下拉選單，輸出卻多了
        # 一張 25 列的大類對照表）。
        "4. SCREENSHOTS — If the page embeds a screenshot of an application (forms, dropdowns, grids),\n"
        "   transcribe the text exactly as it appears inside that screenshot. Do NOT reconstruct,\n"
        "   complete, or expand a partially visible list, and do NOT turn UI widgets into a\n"
        "   reference table that is not on the page.\n\n"
        "5. LISTS — Use `-` for bullets and `1.` `2.` for numbered lists. Indent sub-items with 2 spaces.\n\n"
        "6. IMAGES & DIAGRAMS — For every image, chart, diagram, flowchart, or figure on the page, insert a description block in this exact form:\n"
        "   `[IMAGE: <one-paragraph description: what it shows, key elements, labels, arrows, embedded text>]`\n"
        "   Place the [IMAGE: ...] block where the figure appears in reading order.\n"
        "   NEVER skip an image. Even a small logo or icon gets a brief [IMAGE: ...] line.\n\n"
        "7. INLINE FORMATTING — Use `**bold**` for emphasized labels, backticks for codes/IDs (e.g. `MIL-STD-810G`).\n\n"
        "8. INCLUDE EVERYTHING — All visible text: body, captions, labels, footnotes, headers, page numbers, footers. Preserve original language (do not translate Chinese to English or vice versa).\n\n"
        "9. NO COMMENTARY — Do NOT preface with 'Here is the markdown' or wrap your output in code fences. Output the Markdown content directly."
    )

    for img_bytes, page_num in zip(image_bytes_list, page_numbers):
        try:
            img_bytes = _align14(img_bytes)
            image_b64 = base64.b64encode(img_bytes).decode("utf-8")
            raw = client.chat(
                [
                    {"role": "system", "content": "You are a precise document text extractor."},
                    {"role": "user", "content": prompt, "images": [image_b64]},
                ],
                model=settings.OLLAMA_VISION_MODEL,
            )
            text = strip_markdown_fence_wrapper(raw.strip()) if raw else ""
            text = _strip_prompt_echo(text)
            n_fixed = 0
            if text and native_texts:
                text, n_fixed = repair_code_tokens(text, native_texts.get(page_num, ""))
            if text:
                segments.append({
                    "page": page_num,
                    "paragraph_index": 0,
                    "text": text,
                })
            logger.info("VL extract page=%d text_len=%d 形近字校正=%d", page_num, len(text), n_fixed)
        except Exception as exc:
            logger.warning("VL extract page=%d failed, skipping: %s", page_num, exc)

    return segments


def analyze_pdf_page_images_singleturn(
    image_bytes_list: List[bytes],
    page_numbers: List[int],
    question: Optional[str] = None,
    conversation_history: Optional[List[Dict[str, str]]] = None,
) -> str:
    """Single-turn vision analysis that flattens history into the current prompt.

    This mirrors the stable RAG behavior to avoid reasoning-only responses on
    the first follow-up. It sends a two-message chat (system + user with images)
    and does not include prior turns as chat messages; instead it embeds recent
    Q/A text into the user prompt.
    """
    if not image_bytes_list:
        raise ValueError("no page images provided")

    images = [base64.b64encode(img).decode("utf-8") for img in image_bytes_list]
    page_label = ", ".join(map(str, page_numbers))
    user_prompt = (question or "Summarize the key points from these pages.").strip()

    # Build short textual history (last 3 turns) and concise system rules
    history_text = _compact_vl_history(conversation_history)

    system_rules = (
        "您是專業的文件分析助理，請使用流暢自然的繁體中文（台灣）回答。\n"
        "分析規則（必須全部遵守）：\n"
        "1. 【圖片與圖表】頁面中每一張圖片、示意圖、流程圖、架構圖，都必須主動描述：\n"
        "   - 圖的主題與用途\n"
        "   - 圖中的主要元素、標籤、箭頭關係\n"
        "   - 圖中嵌入的所有文字\n"
        "   範例格式：【圖片說明】此圖為三步驟流程：資料輸入 → 模型運算 → 結果輸出。\n"
        "2. 【表格】保留行列對應關係，整理為清晰條列。\n"
        "3. 【文字內容】提取所有可見文字，包含標題、說明、標籤、附註。\n"
        "4. 針對使用者的問題提供精確回答，但圖片描述不可省略。\n"
        "5. 嚴禁使用簡體中文。\n"
    )

    composed = (
        "上下文 (參考用):\n"
        f"{history_text or '(無)'}\n\n"
        "任務指令:\n"
        f"請分析圖片內容 (頁碼: {page_label})。\n"
        "【重要】頁面中的圖片、圖表、示意圖必須逐一描述，不可略過。\n"
        f"問題：{user_prompt}\n\n"
        "回答 (繁體中文):\n"
    )

    messages = [
        {"role": "system", "content": system_rules},
        {"role": "user", "content": composed, "images": images},
    ]

    return _chat_with_ollama(
        messages,
        model=settings.OLLAMA_VISION_MODEL or settings.OLLAMA_LLM_MODEL,
        options={"num_ctx": _vl_num_ctx(len(images), len(system_rules) + len(composed))},
    )


# 保留給答案生成的 token 額度。VL 的整頁分析動輒 2000-3000 token，
# 若只算輸入就把 num_ctx 用滿，答案會在句子中間被截斷（done_reason=length）。
_VL_OUTPUT_RESERVE = 4096

# 追問時帶入的歷史上限。舊版塞入最近 3 輪的「完整答案」，而整頁分析的答案動輒
# 3,000 字，三輪就約 6,600 字（約 5,600 tokens），把輸出額度吃光。
#
# 策略:「能完整放就完整放，放不下才丟最舊的，截斷是最後手段」。
#
# 預設帶最近 3 輪。若總量超過額度，由最舊往新逐輪捨棄；捨到只剩最新一輪時，
# 那一輪可以獨享全部額度（不再被固定的較小上限綁住）。
# 這樣才不會在還有空間時就把最關鍵的最新一輪切掉 —— 追問銜接的正是它。
# 字數是 token 的近似（中英混雜約 2 字元/token），避免為此載入 tokenizer。
_VL_HISTORY_BUDGET_CHARS = 2600     # 歷史總量上限（字元）
_VL_HISTORY_MAX_TURNS = 3
_TRUNC_NOTE = "…（僅節錄，供銜接語境）"


def _render_turn(turn: Dict[str, str], answer_cap: Optional[int] = None) -> str:
    q = str(turn.get("question") or "").strip()
    a = str(turn.get("answer") or "").strip()
    if answer_cap is not None and len(a) > answer_cap:
        a = a[:answer_cap] + _TRUNC_NOTE
    return f"Q: {q}\nA: {a}\n"


def _compact_vl_history(conversation_history: Optional[List[Dict[str, str]]]) -> str:
    """追問要帶的對話歷史:完整優先、丟舊的次之、截斷最後。

    圖片一律不重複帶入（本來就只送當次選取的頁面），這裡處理的純粹是文字膨脹。
    """
    turns = list((conversation_history or [])[-_VL_HISTORY_MAX_TURNS:])
    if not turns:
        return ""

    # 1) 從最近 N 輪開始，只要總量放得下就全部完整帶入（不截斷任何一輪）
    while turns:
        rendered = [_render_turn(t) for t in turns]
        if sum(len(r) for r in rendered) <= _VL_HISTORY_BUDGET_CHARS or len(turns) == 1:
            break
        turns.pop(0)  # 仍超額 → 丟掉最舊的一輪，再試

    # 2) 只剩最新一輪卻仍超額 → 它獨享全部額度，截斷到剛好放得下
    if len(turns) == 1:
        only = _render_turn(turns[0])
        if len(only) > _VL_HISTORY_BUDGET_CHARS:
            overhead = len(only) - len(str(turns[0].get("answer") or ""))
            cap = max(200, _VL_HISTORY_BUDGET_CHARS - overhead - len(_TRUNC_NOTE))
            return _render_turn(turns[0], answer_cap=cap)
        return only

    return "\n".join(_render_turn(t) for t in turns)


def _vl_num_ctx(n_images: int, text_len: int = 0) -> int:
    """VL 分析所需的 context 大小。

    每頁圖約 1100~1300 tokens（實測 10 張圖 = 10,426），只用預設 8192 會 exceed_context_size。

    text_len 是「提示詞 + 對話歷史」的字元數，必須一起算進來:
    舊版只依圖片數估算，追問時端點會把最近 3 輪的「完整答案」塞進上下文，
    實測 10 張圖 + 約 6,600 字歷史 = 輸入 16,019 tokens，num_ctx 只有 17,096，
    留給輸出僅約 1,100 → done_reason=length，答案在句子中間斷掉。
    這裡改為「圖片 + 文字 + 固定輸出額度」，並保留 _VL_OUTPUT_RESERVE 給生成。
    """
    # 中英混雜文字保守以 ~2 字元/token 估算
    need = n_images * 1300 + max(0, text_len) // 2 + _VL_OUTPUT_RESERVE
    return min(32768, max(settings.OLLAMA_NUM_CTX, need))


def analyze_pdf_page_images_stream(
    image_bytes_list: List[bytes],
    page_numbers: List[int],
    question: Optional[str] = None,
    conversation_history: Optional[List[Dict[str, str]]] = None,
):
    """Streamed version of single-turn analysis.

    Yields dicts: {"type": "thinking"|"content", "text": "..."}

    No-question mode（多頁）: 一頁一次 VL 請求，最後再做一次純文字的跨頁彙整。
    這樣才能確保每一頁都被分析到（模型在單次請求收到多張圖時會自行併頁）。
    Question mode: single request with all images so the answer can span pages.
    """
    if not image_bytes_list:
        raise ValueError("no page images provided")

    client = get_client()
    has_question = bool(question and question.strip())
    user_prompt = question.strip() if has_question else None

    history_text = _compact_vl_history(conversation_history)

    # 依「使用者目的」切換系統規則。判別依據是 conversation_history 是否為空：
    # 前端「分析本頁」固定傳 []，「追問」才會帶歷史（PdfPreviewModal.jsx）。
    # 不能用「有沒有帶問題」判斷——「分析本頁」旁邊也有「想讓 AI 聚焦的問題」欄位。
    is_followup = bool(conversation_history)

    if is_followup:
        # 追問：使用者已看過前面的分析結果，此時要像正常對話一樣針對問題回答，
        # 不該再套完整解析報告的格式，否則每次追問都會重複整頁抽取與制式摘要。
        system_rules = (
            "您是專業的文件分析助理，請使用流暢自然的繁體中文（台灣）回答。\n"
            "這是一次「追問」，使用者已看過先前的分析結果。請遵守：\n"
            "1. 直接針對本次問題作答，不要重複輸出完整頁面解析或制式章節報告。\n"
            "2. 可沿用上下文中已分析過的內容，不需要重新抽取整頁文字。\n"
            "3. 只有在與問題相關時才描述圖片／表格，不必逐一描述頁面上所有圖表。\n"
            "4. 不需附加固定格式的重點摘要；回答長度依問題複雜度自然決定。\n"
            "5. 若頁面資訊不足以回答，請直接說明，不要臆測。\n"
            "6. 嚴禁使用簡體中文。\n"
        )
    elif has_question:
        # 首次分析但使用者指定了聚焦問題：以問題為主軸，仍需描述相關圖表
        #（第一次看這頁，視覺資訊要交代清楚），但不強制整頁逐字抽取與制式摘要。
        system_rules = (
            "您是專業的文件分析助理，請使用流暢自然的繁體中文（台灣）回答。\n"
            "使用者針對這些頁面提出了特定問題。請遵守：\n"
            "1. 以使用者的問題為主軸作答，不要輸出制式章節報告。\n"
            "2. 【圖片與圖表】與問題相關的圖片、示意圖、流程圖須主動描述：主題與用途、\n"
            "   主要元素與箭頭關係、圖中嵌入的文字（含英文標籤）。格式：【圖片說明】...\n"
            "3. 【表格】完整保留行列對應關係，以條列方式呈現相關資料。\n"
            "4. 回答須完整具體，但不需逐字抄錄與問題無關的頁面文字。\n"
            "5. 若頁面資訊不足以回答，請直接說明，不要臆測。\n"
            "6. 嚴禁使用簡體中文。\n"
        )
    else:
        # 首次完整分析（未指定問題）：維持原本的完整抽取規則，搭配下方的章節模板。
        system_rules = (
            "您是專業的文件深度分析助理，請使用流暢自然的繁體中文（台灣）輸出完整分析報告。\n"
            "分析規則（必須全部遵守）：\n"
            "1. 【圖片與圖表】頁面中每一張圖片、示意圖、流程圖、架構圖，都必須主動描述：\n"
            "   - 圖的主題與用途\n"
            "   - 圖中的主要元素、標籤、箭頭關係與邏輯流向\n"
            "   - 圖中嵌入的所有文字（包含英文標籤）\n"
            "   格式：【圖片說明】...\n"
            "2. 【表格】完整保留行列對應關係，以條列方式呈現每筆資料。\n"
            "3. 【文字內容】提取所有可見文字，包含標題、副標題、說明、標籤、備註、頁首頁尾。\n"
            "4. 【重點摘要】每頁結束後，列出 3–5 個該頁的核心重點（條列式）。\n"
            "5. 回答必須完整詳細，不可因篇幅考量而省略任何內容。\n"
            "6. 嚴禁使用簡體中文。\n"
        )

    images = [base64.b64encode(img).decode("utf-8") for img in image_bytes_list]
    page_label = ", ".join(map(str, page_numbers))
    n = len(page_numbers)

    if is_followup:
        # 追問：延續前面的對話，只回答這次問的東西。
        # 即使追問內容為空（前端誤送）也走這裡，避免退回完整模板害整份報告重跑一次。
        followup_text = user_prompt or "請延續上文，補充說明前面尚未提到的重點。"
        task = (
            f"承接上文，請根據頁面（頁碼：{page_label}）的內容回答使用者的追問。\n"
            "只需回答本次問題；與問題無關的頁面內容不必重述。\n"
            f"【追問】{followup_text}\n"
        )
    elif has_question:
        task = (
            f"請根據以下頁面（頁碼：{page_label}）的內容，詳細回答使用者的問題。\n"
            "回答時若頁面中有圖片或圖表，須一併描述說明，不可省略。\n"
            f"【問題】{user_prompt}\n"
        )
    else:
        task = (
            f"請綜合分析以下 {n} 頁文件（頁碼：{page_label}），輸出整合性解析報告，格式如下：\n\n"
            "## 整體主題\n"
            "這幾頁整體在描述什麼？目的與背景為何？\n\n"
            "## 各頁重點\n"
            f"依序說明每一頁（共 {n} 頁）的核心內容，每頁 2–4 條重點。\n\n"
            "## 圖表與視覺資訊\n"
            "描述頁面中重要的圖片、流程圖、架構圖或表格，說明其意義。\n\n"
            "## 跨頁關聯\n"
            "說明各頁之間的邏輯關係、延伸脈絡，或共同指向的結論。\n\n"
            "## 核心重點摘要\n"
            "列出 5–8 條最重要的資訊或結論（條列式）。\n"
        )

    vl_model = settings.OLLAMA_VISION_MODEL or settings.OLLAMA_LLM_MODEL

    # 多頁「整體分析」（未指定問題）改為逐頁請求，最後再彙整跨頁段落。
    #
    # 原本一次把所有圖片送進同一個請求。實測 10 張圖只用掉 10,426 tokens、
    # num_ctx 有 17,096，並沒有超出——但模型仍會自行把 10 頁併成 5 段描述，
    # 還在結尾聲稱「完整涵蓋 10 頁」。使用者挑了 10 頁就是要看 10 頁，
    # 靠提示詞要求模型自律並不可靠，因此改成一頁一次請求（本函式 docstring
    # 原本描述的就是這個設計，只是未被實作）。
    if not has_question and len(images) > 1:
        yield from _stream_per_page_report(
            client, vl_model, images, page_numbers, history_text
        )
        return

    composed = (
        f"上下文（參考用）:\n{history_text or '(無)'}\n\n"
        f"{task}\n"
        "回答（繁體中文）："
    )

    messages = [
        {"role": "system", "content": system_rules},
        {"role": "user", "content": composed, "images": images},
    ]

    for delta in client.chat_stream(
        messages,
        model=vl_model,
        # 把提示詞與對話歷史的長度一起算進來:追問時端點會塞入最近 3 輪的完整答案，
        # 只依圖片數估算會讓輸出額度被吃光，答案在句子中間被截斷。
        options={"num_ctx": _vl_num_ctx(len(images), len(system_rules) + len(composed))},
    ):
        yield delta


_PER_PAGE_RULES = (
    "您是專業的文件分析助理，請使用繁體中文（台灣）分析「單一頁面」。\n"
    "1. 頁面中的圖片、示意圖、流程圖、表格都必須描述：主題與用途、主要元素與\n"
    "   箭頭關係、圖中嵌入的文字（含英文標籤）。\n"
    "2. 表格請保留行列對應關係。\n"
    "3. 條列 2–4 條該頁核心重點。\n"
    "4. 只描述這一頁，不要臆測其他頁面的內容。\n"
    "5. 嚴禁使用簡體中文。\n"
)

_SYNTH_RULES = (
    "您是專業的文件分析助理，請使用繁體中文（台灣）作答。\n"
    "以下是同一份文件多個頁面的逐頁分析結果。請據此彙整跨頁層級的說明，\n"
    "不要重複逐頁細節，也不要加入分析結果中沒有的內容。\n"
)


def _stream_per_page_report(client, vl_model, images, page_numbers, history_text):
    """逐頁分析後彙整：確保每一頁都真的被看過、被寫出來。"""
    per_page_notes: List[str] = []

    for idx, (image_b64, page_num) in enumerate(zip(images, page_numbers), start=1):
        # chat_stream 產生的是 {"type": "content"|"thinking", "text": ...}，
        # 自行插入的文字也要用同樣格式，否則前端 SSE 解析會拿到裸字串。
        yield {"type": "content", "text": f"\n\n## 第 {page_num} 頁\n\n"}
        task = (
            f"請分析這一頁（第 {page_num} 頁，共 {len(images)} 頁中的第 {idx} 頁）的內容。\n"
            "回答（繁體中文）："
        )
        buf: List[str] = []
        try:
            for delta in client.chat_stream(
                [
                    {"role": "system", "content": _PER_PAGE_RULES},
                    {"role": "user", "content": task, "images": [image_b64]},
                ],
                model=vl_model,
                options={"num_ctx": _vl_num_ctx(1)},
            ):
                # 彙整時只需要正文，thinking 不納入（會污染後續的跨頁摘要）
                if isinstance(delta, dict) and delta.get("type") == "content":
                    buf.append(str(delta.get("text") or ""))
                yield delta
        except Exception as exc:  # noqa: BLE001
            # 單頁失敗不中斷整份分析，明確標記讓使用者知道哪一頁沒成功
            msg = f"（第 {page_num} 頁分析失敗：{exc}）"
            logger.warning("per-page VL failed page=%s: %s", page_num, exc)
            yield {"type": "content", "text": msg}
            buf.append(msg)
        per_page_notes.append(f"[第 {page_num} 頁]\n{''.join(buf).strip()}")

    # 跨頁彙整：純文字請求，不再送圖片
    yield {"type": "content", "text": "\n\n---\n"}
    notes = "\n\n".join(per_page_notes)
    synth_task = (
        f"上下文（參考用）:\n{history_text or '(無)'}\n\n"
        f"逐頁分析結果：\n{notes}\n\n"
        "請輸出以下三個段落：\n\n"
        "## 整體主題\n這幾頁整體在描述什麼？目的與背景為何？\n\n"
        "## 跨頁關聯\n說明各頁之間的邏輯關係、延伸脈絡，或共同指向的結論。\n\n"
        "## 核心重點摘要\n列出 5–8 條最重要的資訊或結論（條列式）。\n\n"
        "回答（繁體中文）："
    )
    try:
        for delta in client.chat_stream(
            [
                {"role": "system", "content": _SYNTH_RULES},
                {"role": "user", "content": synth_task},
            ],
            model=vl_model,
            options={"num_ctx": min(32768, 4096 + len(notes) // 2)},
        ):
            yield delta
    except Exception as exc:  # noqa: BLE001
        logger.warning("cross-page synthesis failed: %s", exc)
        yield {"type": "content", "text": f"\n（跨頁彙整失敗：{exc}）"}


def finalize_text_answer(notes: str, question: Optional[str] = None) -> str:
    """Turn noisy notes/thinking into a clean final answer (text-only).

    Used as a post-stream rescue when the model streamed only thinking or
    extremely short content. This mirrors RAG's single-turn text generation.
    """
    if not notes:
        notes = "(no notes)"

    system = (
        "You are a helpful technical assistant. Convert the following noisy notes "
        "into a concise final answer.\n"
        "Rules:\n"
        "- Output final answer only. MUST use Traditional Chinese (繁體中文) for all Chinese text.\n"
        "- NO Simplified Chinese allowed.\n"
        "- Bullet points preferred, no control tokens, no chain-of-thought.\n"
        "- If insufficient info, state what is missing.\n"
    )
    task = (
        (f"Question: {question}\n" if question else "") +
        "Notes:\n" + notes.strip() + "\n\n" +
        "Final answer (bulleted if applicable):"
    )

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": task},
    ]
    return _chat_with_provider(messages)

def analyze_pdf_page_images(
    image_bytes_list: List[bytes],
    page_numbers: List[int],
    question: Optional[str] = None,
    conversation_history: Optional[List[Dict[str, str]]] = None,
) -> str:
    if not image_bytes_list:
        raise ValueError("沒有可用的頁面圖片")

    messages = _prepare_history(conversation_history)
    user_prompt = question or "請描述這些頁面中的重點內容。"

    images = [base64.b64encode(img).decode("utf-8") for img in image_bytes_list]
    page_label = ", ".join(map(str, page_numbers))
    try:
        logger.info(
            "Vision analyze: pages=%s images=%d question_len=%d history_turns=%d",
            page_numbers,
            len(images),
            len(user_prompt or ""),
            len(conversation_history or [])
        )
    except Exception:
        pass
    messages.append(
        {
            "role": "user",
            "content": (
                "以下圖片為 PDF 內容的截圖，可能與實際頁碼或視窗顯示不同步。"
                f"請僅根據影像中的文字與圖表回答問題，忽略任何頁碼差異。"
                f"當前對應的頁面標記：{page_label}。請回答：{user_prompt}"
            ),
            "images": images,
        }
    )

    # Add system guidance as the first turn to reduce repetition and keep outputs concise
    system_rules = (
        "您是專業的文件分析助理，請使用流暢自然的繁體中文（台灣）回答。\n"
        "任務目標：\n"
        "1. 仔細閱讀圖片中的文字與圖表。\n"
        "2. 針對使用者的問題提供精確、重點式的回答。\n"
        "3. 若遇到表格或數據，請整理為清晰的條列式重點。\n"
        "4. 保持語句通順，避免贅字或重複詞彙。\n"
    )
    messages.insert(0, {"role": "system", "content": system_rules})

    return _chat_with_ollama(
        messages,
        model=settings.OLLAMA_VISION_MODEL or settings.OLLAMA_LLM_MODEL,
    )
