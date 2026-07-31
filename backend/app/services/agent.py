"""
ReAct-style Agent loop.

The LLM emits JSON of one of two shapes per step:
  {"thought": "...", "action": "tool_name", "action_input": {...}}
  {"thought": "...", "final_answer": "..."}

We parse the JSON (strict), execute the tool, append observation, repeat
until final_answer or max_steps. Bad JSON gets one retry with a stricter
instruction; second failure terminates with the raw text as final answer.

Yields a stream of events for SSE consumption.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Generator, List, Optional

from sqlalchemy.orm import Session

from . import agent_tools, ai
from ..core.config import settings
from .llm_provider import get_llm_provider
from .system_config import SystemConfigService

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT_TEMPLATE = """You are a precise document-research agent for a knowledge base of \
technical standards (ISO / IEC / MIL-STD / IEEE / ASTM / JIS / CNS / etc).

You answer the user's question by calling tools step-by-step. Each step you \
output ONE JSON object — no prose, no markdown fences.

Two output shapes:
  {{"thought": "<one-sentence reasoning>", "action": "<tool_name>", "action_input": {{...}}}}
  {{"thought": "<one-sentence reasoning>", "final_answer": "<answer in user's language>"}}

Available tools:
{tools}

Rules:
- Output ONE JSON object per turn, nothing else.
- Stay ANCHORED to the user's original subject. You MAY rephrase a query for better \
retrieval (drop filler words, extract keywords, add synonyms), but you must NOT switch to a \
different topic than what was asked. Retrieved snippets are EVIDENCE, not a redirection — if \
a search surfaces a similarly-named but different item, do not pivot to it. Your final_answer \
must answer the ORIGINAL question's subject.
- Use spec_lookup BEFORE spec_references / spec_supersedes_chain to resolve canonical_id.
- For "what standards/specs does METHOD or section X reference/cite" ("方法/§X 引用了哪些規範/標準"), \
call section_lookup(name) — test methods and numbered sections are graph nodes with an EXACT \
reference list (rag_search is vague and misses the reference table). e.g. section_lookup("Method 510.7").
- When the user asks ABOUT a specific standard by its id ("IEC 60068-2-68 是什麼", "what is ASTM D185-07"), \
call spec_lookup with that EXACT id FIRST to resolve the right node (do NOT let rag_search confuse it with a \
similarly-numbered standard, e.g. IEC 60068-2-68 vs IEC 68-2-52). The corpus contains the referenced \
standard's TITLE (from this document's reference list) but usually NOT its full text — give the title/scope \
if found and state plainly that the standard's full content is not in the knowledge base; do NOT fabricate it.
- For enumeration / listing questions ("what items/tests does X have", "有哪些", "list all", \
"子項目", "sub-tests of X"), call list_subitems(name) FIRST. It returns the COMPLETE set from \
the knowledge graph; rag_search alone retrieves only top-k chunks and WILL miss items.
- For a question ABOUT a whole category/group ("介紹/說明 X", "tell me about the X tests", \
"these tests", "X 的內容/重點"), or a FOLLOW-UP elaborating on a group, call coverage_check(name) \
to get EVERY sub-item with content. rag_search ranks by keyword/similarity and WILL miss \
sub-items whose pages don't contain the keyword (e.g. a 'Surface Deflection' sub-test under \
'Pressure Test'). If a rag_search for a category looks partial, follow up with coverage_check \
and merge — the final answer must cover ALL sub-items, not only the ones rag_search surfaced.
- If a tool returns no relevant info, try another tool or a different query — do NOT \
hallucinate spec content. If the corpus truly lacks info, say so in final_answer.
- Do NOT settle for a "the test plan must specify X" answer. Standards documents have an \
"INFORMATION REQUIRED" section that merely NAMES the parameters (salt solution pH, fallout \
rate, air velocity...) and a separate procedure section that holds the ACTUAL VALUES. If your \
evidence only names parameters without values, you have NOT answered — take the parameter \
names from that list and run another rag_search for each (English terms work better: the \
corpus is English), or search the method's procedure/step sections, before finishing.
- When the user asks for conditions, parameters, limits, durations or tolerances, the answer \
MUST contain concrete numbers with units. Keep searching with different wording until you \
have them, or state explicitly which value is missing and where it likely lives.
- Emit final_answer once the evidence genuinely answers the question. Brevity is preferred for \
SINGLE-item questions; but for category/group questions completeness wins — make sure every \
sub-item (per list_subitems / coverage_check) is represented before finishing.
- Always answer in the user's language (zh-TW if they wrote Chinese; English otherwise).
- Cite document titles or spec canonical_ids inline when summarizing findings.

Max {max_steps} steps. After that, you MUST emit final_answer."""


# ── 參數題補救：判斷「使用者要數值」與「答案沒給數值」───────────────────────
# 補救輪數。實測使用者需要追問 2-3 次才拿到數值，那些輪次本來就該由 agent 自己跑完。
_PARAM_RETRY_MAX = 3

# 問題在要具體條件/數值
_WANTS_VALUE_RE = re.compile(
    # 只認「明確索取數值」的線索。
    #
    # 不能把「溫度 / 濕度」這類詞單獨列入 —— 它們同時是測試項目的名稱，
    # 「濕度測試的目的是什麼」也會命中，於是純敘述的答案（本來就不該有數值）
    # 被判為不足，白跑 3 輪補救。要求它們與索取字樣搭配才算。
    #
    # 「範圍」同理有兩義:「溫度範圍」是數值區間、「文件的範圍」是 scope。
    r"條件|參數|規格|多少|數值|幾度|幾小時|幾分鐘|公差|限值|標準值|門檻"
    r"|(?:溫度|濕度|濃度|頻率|壓力|加速度|時間|速率|流速|電壓|電流)"
    r"\s*(?:是多少|要求|條件|範圍|值|規定|上限|下限)"
    # 英文問法用寬鬆比對：「what is the salt concentration」中間會夾字。
    r"|how\s+(much|long|many)"
    r"|what.{0,24}(value|temperature|concentration|rate|duration|limit|tolerance|pressure)"
    r"|parameter|condition|requirement|tolerance|threshold",
    re.I,
)
# 帶單位的具體數值（規範常用單位）
_VALUE_TOKEN_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|°\s?[CF]|º[CF]|℃|℉|m/s|ft/min|g/m3|g/m³|g/ft3|ml/cm|kPa|hPa|psi|Hz|dB"
    r"|mm|cm|inch|英寸|小時|分鐘|秒|天|hours?|minutes?|days?)",
    re.I,
)
# 「沒給數值」的典型措辭。實測補上原本漏掉的講法:
# 「需根據運輸、儲存與部署時的氣候…來確定」（需/應 與 確定 之間可以很長）、
# 「應選取最壞情況」、「可參考表 507.6-I」（只指路不給值）、「視情況而定」。
_VAGUE_RE = re.compile(
    r"需(根據|依|在).{0,30}?(指定|規定|設定|確定|決定)"
    r"|[應需].{0,20}?選(取|擇).{0,10}(最壞|適當|合適)"
    r"|[可請]參考表|詳見表|見表\s*[\d.]"
    r"|視.{0,8}而定|依.{0,8}而定"
    r"|需明確規定|由測試計畫|依測試計畫|未明確|未提供具體"
)

# 同一句話被重複貼上多次 —— 實測「我要測試條件」的答案把同一句
# 「需根據運輸、儲存與部署…最壞情況」重複了 5 次，這種答案對使用者毫無價值。
_MIN_REPEAT_LEN = 24
_MAX_REPEAT_TIMES = 2


def _has_heavy_repetition(answer: str) -> bool:
    """答案是否大量重複同一段長句。"""
    lines = [re.sub(r"\s+", "", ln) for ln in (answer or "").splitlines()]
    seen: Dict[str, int] = {}
    for ln in lines:
        ln = re.sub(r"\[來源\d+\]", "", ln)
        if len(ln) >= _MIN_REPEAT_LEN:
            seen[ln] = seen.get(ln, 0) + 1
            if seen[ln] > _MAX_REPEAT_TIMES:
                return True
    return False

# 直接放棄的答案。實測 agent 有時在第 1 步就交出「查無相關資料」——
# 明明語料裡有（同一題再問一次就答得出來），只是 seed 檢索該次撈到不相關的段落。
_NO_ANSWER_RE = re.compile(
    r"查無相關資料|查無資料|沒有找到|無法回答|找不到.{0,6}(資料|內容)|no relevant|not found",
    re.I,
)


# 問題是否指名了查詢對象。缺少對象時（例如只說「找出測試條件」），
# 語料裡 20 幾種測試方法各有自己的條件，任何答案都是任意挑的 —— 應該反問而非放棄。
_SUBJECT_RE = re.compile(
    r"MIL-STD|MIL-DTL|MIL-PRF|MIL-HDBK|ISO|IEC|IEEE|ASTM|Method\s*\d|方法\s*\d|\d{3}\.\d"
    r"|高溫|低溫|溫度衝擊|濕度|鹽霧|砂塵|沙塵|振動|震動|衝擊|加速度|太陽|輻射|黴菌|真菌"
    r"|淋雨|降雨|低壓|高度|爆炸|酸性|結冰|凍雨|運輸|噪音|彈跳|落下|傾斜",
    re.I,
)


# 「X測試／X試驗」的通用式主體。_SUBJECT_RE 是寫死的測試類型白名單，一定會有漏網：
# 實測「冷凝測試方法與條件」因為白名單沒有「冷凝」而被判成無主體，於是繼承了
# 上一輪的「濕度」去查 —— 使用者問的明明是另一項測試。
# 這裡不再要求測試名稱事先登錄，只排除「我要／找出」這類意圖詞。
_XTEST_RE = re.compile(r"([一-鿿]{2})(?:測試|試驗)")
_GENERIC_PREFIX = {
    "我要", "我想", "找出", "列出", "請問", "想要", "幫我", "麻煩", "這個", "那個",
    "進行", "執行", "做的", "相關", "所有", "全部", "什麼", "哪些", "本次", "這次",
    "以上", "上述", "後續", "其他", "另外",
}


def _find_subject(text: str) -> Optional[str]:
    """取出文字中的查詢對象（規範編號／方法號／測試類型），找不到回 None。"""
    m = _SUBJECT_RE.search(text or "")
    if m:
        return m.group(0)
    for m in _XTEST_RE.finditer(text or ""):
        if m.group(1) not in _GENERIC_PREFIX:
            return m.group(1)
    return None


def _lacks_subject(question: str) -> bool:
    """問題是否沒有指名查詢對象（測試類型、規範編號、方法號）。"""
    return _find_subject(question) is None


def _history_subject(conversation_history: Optional[List[Dict[str, Any]]]) -> Optional[str]:
    """從最近的對話取出查詢對象，供「主體省略型追問」繼承。

    例:上一輪問「振動測試的測試條件」，這一輪只說「找出測試條件」——
    使用者顯然還在談振動測試，應該繼承主體去查，而不是反問或回查無資料。
    優先取使用者自己的提問（比答案更能代表意圖）。
    """
    for turn in reversed((conversation_history or [])[-3:]):
        for field in ("question", "answer"):
            found = _find_subject(str(turn.get(field) or "")[:300])
            if found:
                return found
    return None


def _clarify_scope_text(db: Session, question: str) -> str:
    """問題缺少查詢對象時的反問內容。

    子項目清單直接取自 KG（list_subitems），不寫死方法名稱，換語料也適用。
    """
    options: List[str] = []
    try:
        obs = agent_tools.run_tool(db, "list_subitems", {"name": "MIL-STD-810H"})
        if isinstance(obs, dict):
            for it in (obs.get("subitems") or [])[:12]:
                label = str(it.get("name") or it.get("canonical_id") or "").strip()
                if label:
                    options.append(label)
    except Exception as e:  # noqa: BLE001
        logger.debug("clarify list_subitems failed: %s", e)

    lines = [
        "你的問題沒有指定要查哪一項測試，我需要先確認範圍再查，否則只能任意挑一項給你。",
        "",
        "這批文件涵蓋多種測試方法，每一種都有各自的條件與數值。",
    ]
    if options:
        lines += ["", "可選的項目（節錄）："] + [f"- {o}" for o in options]
    lines += [
        "",
        "請補上測試類型或規範編號，例如：",
        "- 振動測試（Method 514）的測試條件是什麼",
        "- 鹽霧測試的鹽溶液濃度與 pH 要求",
        "- 低溫測試的溫度與持續時間",
    ]
    return "\n".join(lines)


def _is_no_answer(text: str) -> bool:
    """是否為「直接放棄」的答案。

    判準是「放棄字樣出現在開頭」而非只看長度：中文資訊密度高，用長度當門檻會把
    「正文有數值、只是附帶提到某項查無資料」的正常答案誤判成失敗。
    真正放棄的答案是以「查無相關資料」開場的。
    """
    if not text or not text.strip():
        return True
    head = text.strip()[:60]
    return bool(_NO_ANSWER_RE.search(head))


# 「怎麼做」型問句。在測試規範的語境下，問某項測試的方法/程序/步驟等同於在要參數——
# 一份沒有溫度、濕度、持續時間的「測試方法」是無法執行的。實測使用者正是從
# 「濕度測試方法」問起，拿到一段只講 Procedure I/II 名稱、零數值的敘述，才被迫追問。
_HOWTO_RE = re.compile(
    r"測試方法|方法|程序|步驟|怎麼(做|測|進行|執行)|如何(做|測|進行|執行)|procedure|how\s+to",
    re.I,
)
# 目的／適用範圍題：這類問題的正確答案本來就沒有數值，不可因為「零數值」而深查。
_PURPOSE_RE = re.compile(
    r"目的|用途|適用|不適用|為什麼|為何|定義|意義|purpose|scope|why|definition",
    re.I,
)


def _wants_values(question: str) -> bool:
    q = question or ""
    # 明確索取數值的線索優先於任何否決：「溫度範圍是多少」含「範圍」，
    # 但它問的是數值區間，不是文件的 scope。
    if _WANTS_VALUE_RE.search(q):
        return True
    if _PURPOSE_RE.search(q) or re.search(r"(?<![溫濕濃頻壓時速電])範圍", q):
        return False
    # 關係題（「哪些方法引用了 X」）與列舉題（「有哪些測試方法」）都含「方法」二字，
    # 但它們走 KG 確定性路徑，答案是清單而非數值 —— 補救迴圈與反問對它們都沒有意義。
    if _RELATION_RE.search(q) or _ENUM_STRUCT_RE.search(q):
        return False
    return bool(_HOWTO_RE.search(q))


def _lacks_values(answer: str) -> bool:
    """答案是否缺乏具體數值（僅在 _wants_values 成立時才會被問到）。

    原本要求「數值 <=2 且空話 >=2」兩個條件同時成立，實測漏掉最該抓的案例:
    「我要測試條件」的答案「一個帶單位的數值都沒有」，卻因為空話正則沒涵蓋
    當時的措辭（需根據…來確定 / 應選取最壞情況 / 可參考表 507.6-I）而被判為足夠。
    使用者明確在要數值、答案卻完全沒有數值，這本身就足以判定不足，不需要佐證。
    """
    if not answer:
        return True
    n_values = len(set(_VALUE_TOKEN_RE.findall(answer)))
    if n_values == 0:
        return True
    if _has_heavy_repetition(answer):
        return True
    n_vague = len(_VAGUE_RE.findall(answer))
    return n_values <= 2 and n_vague >= 2


def _is_degenerate(answer: str) -> bool:
    """答案是否「明顯沒有交代任何實質內容」，與問題怎麼問無關。

    為什麼需要這一關：`_wants_values` 只認明確索取數值的措辭，
    但實測使用者是從「濕度測試方法」開始問的 —— 那句不算在要數值，
    可是模型交回的答案零數值、同一句重複 5 次、通篇「需依…而定」。
    這種答案無論問題意圖如何都該自己再查一輪，不該讓使用者追問。

    門檻刻意比 `_lacks_values` 嚴（要同時滿足零數值 + 空話/重複），
    否則「本方法的目的與適用範圍」這類本來就沒有數值的正常答案會被誤判。
    """
    if not answer:
        return False  # 空答案走 `_is_no_answer` 的放棄補救路徑
    if len(set(_VALUE_TOKEN_RE.findall(answer))) > 0:
        return False
    return _has_heavy_repetition(answer) or len(_VAGUE_RE.findall(answer)) >= 2


# 規範裡「需參照表 X」「見 Cycle B1」這類措辭，本身就標示了數值的所在地。
_TABLE_REF_RE = re.compile(r"(?:table|表)\s*(\d{3}\.\d+-[IVX]+\b|\d{3}\.\d+-\d+\b)", re.I)
_CYCLE_REF_RE = re.compile(r"\b(cycle\s+[A-C]\d?)\b", re.I)


def _extract_param_terms(evidence: List[Dict[str, Any]], answer: str) -> List[str]:
    """從證據與答案中撈出下一輪查詢的種子關鍵字。

    優先序有講究：
      1. 表號（Table 507.6-I）與循環代號（Cycle B1）—— 規範說「條件見表 X」時，
         數值就在那張表所屬的段落。實測這是唯一能把 66ºC / 71ºC 撈出來的路徑。
      2. INFORMATION REQUIRED 章節的條列參數名稱，例如
         "(a) Salt solution pH. (b) Salt solution fallout rate (ml/cm2/hr)."

    先前只有第 2 項，結果在濕度題撈到的是「Presence of free water」這類
    「濕度造成的影響」清單 —— 那是現象描述，不是數值所在地，重查等於白跑。
    """
    blob = " ".join(
        (e.get("snippet") or e.get("text") or "") for e in evidence
    ) + " " + (answer or "")
    terms: List[str] = []

    def _add(t: str) -> None:
        t = re.sub(r"\s+", " ", t).strip(" .")
        if t and t.lower() not in {x.lower() for x in terms}:
            terms.append(t)

    for m in _TABLE_REF_RE.finditer(blob):
        _add(f"Table {m.group(1).upper()}")
    for m in _CYCLE_REF_RE.finditer(blob):
        _add(m.group(1).title())

    # (a) Xxx yyy.  /  (1) Xxx yyy.  形式的條列項目
    # 字元類要含數字，否則 "Salt solution fallout rate (ml/cm2/hr)" 會被切斷而漏掉
    for m in re.finditer(r"\([a-z0-9]\)\s*([A-Z][A-Za-z0-9 /()\-]{6,60}?)\s*[.\n]", blob):
        t = re.sub(r"\s+", " ", m.group(1)).strip(" .")
        if 6 <= len(t) <= 60:
            _add(t)
    return terms[:6]


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def _parse_step(text: str) -> Optional[Dict[str, Any]]:
    """Return parsed step dict or None if no valid JSON found."""
    if not text:
        return None
    text = text.strip()

    # Try fenced code block first
    fence = _JSON_FENCE_RE.search(text)
    candidates: List[str] = []
    if fence:
        candidates.append(fence.group(1).strip())
    # Then first top-level {...} object
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        candidates.append(m.group(0))

    for raw in candidates:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue
    return None


def _build_history_messages(conversation_history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Compress recent QA history into chat messages for context."""
    msgs: List[Dict[str, Any]] = []
    for entry in (conversation_history or [])[-4:]:
        q = (entry.get("question") or "").strip()
        a = (entry.get("answer") or "").strip()
        if q:
            msgs.append({"role": "user", "content": q})
        if a:
            msgs.append({"role": "assistant", "content": a[:1500]})
    return msgs


def _needs_deeper_search(question: str, answer: str) -> bool:
    """答案是否還不足以交給使用者（兩條路徑，見 `_wants_values` / `_is_degenerate`）。"""
    if not answer:
        return False
    return (_wants_values(question) and _lacks_values(answer)) or _is_degenerate(answer)


def _grounded_synthesis(
    db: Session,
    question: str,
    rag_evidence: List[Dict[str, Any]],
    kg_notes: List[str],
    conversation_history: Optional[List[Dict[str, Any]]],
    *,
    ensure_values: bool = True,
) -> tuple:
    """外層包裝：合成一次，若答案缺數值就自己補查再合成，直到夠用或試完。

    為什麼放在這裡而不是流程末端：`run_agent` 有 9 個「產出最終答案」的出口，
    其中 8 個會提早 return，繞過末端的補救迴圈。實測同一個問題在不同輪次會走到
    不同出口（LLM 每次挑的工具不同），於是「有時會自己深查、有時不會」。
    所有出口的內容答案都出自這個函式，把補救放進來才是唯一覆蓋得完的位置。

    `ensure_values=False` 供內部輔助呼叫使用（子問題拆解、摘要生成）——
    那些不是要交給使用者的最終答案，不值得多花幾輪檢索。
    """
    ans, sources, n_used, n_total = _synthesize_grounded(
        db, question, rag_evidence, kg_notes, conversation_history)
    if not ensure_values:
        return ans, sources, n_used, n_total

    evidence = list(rag_evidence)
    for _ in range(_PARAM_RETRY_MAX):
        if not _needs_deeper_search(question, ans):
            break
        terms = _extract_param_terms(evidence, ans)
        if not terms:
            break
        seen = {(e.get("document_id"), e.get("page"),
                 (e.get("snippet") or e.get("text") or "")[:48]) for e in evidence}
        fresh: List[Dict[str, Any]] = []
        for term in terms[:4]:
            try:
                more, _ = _seed_evidence_via_rag(db, f"{term} value requirement", top_k=5)
            except Exception as e:  # noqa: BLE001
                logger.warning("ensure_values 補查「%s」失敗: %s", term, e)
                continue
            for ev in more:
                k = (ev.get("document_id"), ev.get("page"),
                     (ev.get("snippet") or ev.get("text") or "")[:48])
                if k not in seen:
                    fresh.append(ev)
                    seen.add(k)
        if not fresh:
            break
        # 補回來的證據放最前面，且帶數值者優先 —— 合成受預算限制會截尾，
        # 接在尾端等於白查（實測撈到了頁 212 的 66ºC/71ºC 卻被截掉）。
        fresh.sort(key=lambda e: 0 if _VALUE_TOKEN_RE.search(
            e.get("snippet") or e.get("text") or "") else 1)
        evidence[:0] = fresh
        new_ans, new_sources, new_used, new_total = _synthesize_grounded(
            db, question, evidence, kg_notes, conversation_history)
        if new_ans:
            ans, sources, n_used, n_total = new_ans, new_sources, new_used, new_total
    return ans, sources, n_used, n_total


def _synthesize_grounded(
    db: Session,
    question: str,
    rag_evidence: List[Dict[str, Any]],
    kg_notes: List[str],
    conversation_history: Optional[List[Dict[str, Any]]],
) -> tuple:
    """Phase 0：用已調好的 RAG grounding prompt 重新生成最終答案。

    回傳 (answer, n_rag_used, n_rag_total_unique)，後兩者供 Phase 3 完整度反問使用。

    把 ReAct 過程蒐集到的 rag_search 命中段落（含 title/page）+ KG 關聯整理成
    編號 context，套用「只能依段落、標 [來源]、不可跨來源拼湊」的 RAG 模板再生成一次，
    讓 Agent 答案具備 RAG 等級的引用與防幻覺紀律。總長度受 num_ctx 預算限制。
    """
    budget = ai.effective_rag_budget()

    # 先去重，算出「實際有幾筆不重複的檢索證據」(total_unique)，供 Phase 3 完整度判斷
    deduped: List[tuple] = []
    seen: set = set()
    for ev in rag_evidence:
        text = (ev.get("snippet") or ev.get("text") or "").strip()
        if not text:
            continue
        key = (ev.get("document_id"), ev.get("page"), text[:80])
        if key in seen:
            continue
        seen.add(key)
        deduped.append((ev, text))
    total_unique = len(deduped)

    # 在預算內塞入 context；裝不下的就是「檢索到但未展開」的來源
    contexts: List[Dict[str, Any]] = []
    used_sources: List[Dict[str, Any]] = []  # 供前端可點/可預覽的結構化來源（對齊 RAG）
    used = 0
    for ev, text in deduped:
        if used + len(text) > budget:
            text = text[: max(0, budget - used)]
        if not text:
            break
        contexts.append({
            "source_num": len(contexts) + 1,
            "title": ev.get("title") or "",
            "page": ev.get("page"),
            "page_gap": None,
            "text": text,
        })
        used_sources.append({
            "document_id": ev.get("document_id"),
            "title": ev.get("title") or "",
            "page": ev.get("page"),
            "snippet": ev.get("snippet") or text,
            "score": ev.get("score"),
        })
        used += len(text)
        if used >= budget:
            break
    n_rag_used = len(contexts)

    # 把 KG / spec 關聯當作額外一個來源塊（若還有預算）
    if kg_notes and used < budget:
        note_text = ("關聯資訊（知識圖譜）:\n" + "\n".join(kg_notes))[: max(0, budget - used)]
        if note_text.strip():
            contexts.append({
                "source_num": len(contexts) + 1,
                "title": "知識圖譜關聯",
                "page": None,
                "page_gap": None,
                "text": note_text,
            })

    if not contexts:
        return None, [], 0, total_unique

    prompts = SystemConfigService(db).get_rag_prompts()
    answer = ai.generate_rag_answer(
        question,
        contexts,
        conversation_history=conversation_history,
        system_prompt=prompts["system_prompt"],
        user_template=prompts["user_template"],
    )
    return answer, used_sources, n_rag_used, total_unique


def _coverage_note(n_unused_sources: int, kg_edges_seen: int) -> str:
    """Phase 3：依啟發式產生「還有更多、要不要深掘」的反問（不額外呼叫 LLM）。

    只在「確實還有未展開的檢索來源或圖譜關聯」時才附加，避免每次都問。
    """
    bits: List[str] = []
    if n_unused_sources > 0:
        bits.append(f"另有約 {n_unused_sources} 段相關內容因長度限制未在本次展開")
    if kg_edges_seen > 0:
        bits.append(f"知識圖譜中已找到 {kg_edges_seen} 條規範關聯，可再往上追引用鏈／版本鏈")
    if not bits:
        return ""
    return "\n\n---\n📌 " + "；".join(bits) + "。需要我針對哪一項深入查嗎？"


_ENUM_RE = re.compile(
    r"(有哪些|哪些|列出|列舉|清單|子項目|有什麼|包含哪些|底下有|下有|"
    r"sub[- ]?items?|list all|list the|what .*(items|tests|sub)|which .*(items|tests))",
    re.IGNORECASE,
)


def _looks_like_enumeration(q: str) -> bool:
    """判斷是否為列舉題（有哪些/列出/子項目…），用於確定性 KG 列舉 fallback。"""
    return bool(_ENUM_RE.search(q or ""))


# 混合查詢路由：關係/引用/版本/結構類 → 走 Agent（KG 工具有用、且 Agent 在這類是 RAG 超集）；
# 純內容（怎麼進行/數值/條件/是什麼）→ 走純 RAG（更快、不會被 KG 工具淡化答案）。
# Audit H16：英文詞全部補上 \b，否則 requires 命中 "required"、version 命中
# "conversion"、deriv 命中 "derivative"、replace 命中 "replacement"，把純內容題誤判成關係題。
# 另補 successor/後繼/下一版（原本漏了關係題的正向）。
# 移除單獨的「參考/參照/依據」：它們是中文內容題的常見開頭（「依據 810H，514.6 加速度多少？」），
# 誤判率高；真正的關係題會用「引用/取代/版本/哪些規範」，仍會被下面的詞捕捉。
_RELATION_RE = re.compile(
    r"(取代|被取代|沿革|演進|演化|版本|舊版|新版|前一?版|下一?版|後繼|衍生|引用|關係|"
    r"引用鏈|版本鏈|誰引用|被.*引用|哪些(規範|標準|文件|method|方法)|跟.*關係|和.*關係|與.*關係|"
    r"\bsupersed|\breplace|\bderiv|\breferences?\b|\bcite[sd]?\b|\brequires?\b|\bversion\b|"
    r"\brevision\b|\blineage\b|\bpredecessor\b|\bsuccessor\b|\brelationship\b)",
    re.IGNORECASE,
)


# 結構列舉題（→ Agent 的 list_subitems）：要求列舉「結構性名詞」（子測試/方法/程序/章節…），
# 刻意排除「哪些數值/資訊/數據/條件」這類其實是內容題的問法（那些走 RAG 更好）。
_ENUM_STRUCT_RE = re.compile(
    r"((子|sub-?\s?)(項目|測試|程序|test|item|procedure)|列出所有|全部列出|列舉|"
    r"有哪些(測試|方法|程序|步驟|項目|子|method|annex|附錄|章節))",
    re.IGNORECASE,
)


# 程序列舉題：「(Method X) 有哪些程序 / 程序有哪些 / what procedures」
_PROC_Q_RE = re.compile(r"((有)?哪些|列出|列舉|what|which).{0,12}(程序|procedure)s?|(程序|procedure)s?\s*(有哪些|是什麼)", re.IGNORECASE)
# 應用題（「我的設備該做哪些測試」）：不要被 spec 關係區塊劫持，交給合成。
# Audit H17：收窄。舊版單獨的「產品/設備/安裝」名詞出現就 is_app=True → 連
# 「MIL-STD-810H 對設備的鹽霧測試引用哪些標準？」這種關係題也被封殺所有 KG 確定性答案。
# 改為只認帶「使用者意圖」的措辭（我的/我們/該做/要符合/裝在/用在…），不再被裸名詞觸發。
_APP_RE = re.compile(r"我的|我們|我要|該做|應(該|做)|需要做|裝在|用在|要符合|要不要做|該不該做")
# 規範 id（用於確定性規範關係 fallback）：MIL-STD/HDBK/DTL/PRF、AR、ASTM、IEC、ISO、NATO STANAG
_SPEC_ID_RE = re.compile(
    r"\b(MIL-(?:STD|HDBK|DTL|PRF)-\d+[A-Z]?|AR\s?\d+-\d+|ASTM\s?[A-Z]?\d+|IEC\s?\d+(?:-\d+)*|ISO\s?\d+|NATO\s?STANAG\s?\d+)\b",
    re.IGNORECASE,
)


def route_mode(question: str) -> str:
    """混合模式分類：回傳 'agent'（關係/引用/版本/結構列舉題）或 'rag'（純內容題）。"""
    q = question or ""
    if _RELATION_RE.search(q) or _ENUM_STRUCT_RE.search(q):
        return "agent"
    return "rag"


def run_rag_only(db: Session, question: str,
                 conversation_history: Optional[List[Dict[str, Any]]] = None):
    """混合路由的純 RAG 分支：與 /rag/query 同一條檢索 + grounded 合成，回 (answer, sources)。"""
    seeded, _conf = _seed_evidence_via_rag(db, question, top_k=5)
    ans, sources, n_used, n_total = _grounded_synthesis(db, question, seeded, [], conversation_history)
    if ans and ans.strip():
        note = _coverage_note(max(0, n_total - n_used), 0)
        return ans.strip() + (note or ""), sources
    closest = [{"title": ev.get("title"), "page": ev.get("page"), "text": ev.get("snippet")}
               for ev in seeded[:3]]
    low_src = [{"document_id": ev.get("document_id"), "title": ev.get("title"), "page": ev.get("page"),
                "snippet": ev.get("snippet"), "score": ev.get("score")} for ev in seeded[:5]]
    text = ai.low_confidence_answer(question, closest) if closest else "查無足夠的相關內容，請提供更多文件或調整問題。"
    return text, low_src


def _pick_spec_center(candidates: List[str], question: str) -> Optional[str]:
    """從查過的規範中挑「問題裡實際提到的那個」當主體，避免被後續查的別的規範蓋掉
    （如問 MIL-DTL-901 卻因 agent 又查了 MIL-STD-810H 而把主體變成 810H）。"""
    if not candidates:
        return None
    qn = re.sub(r"[^A-Z0-9]", "", (question or "").upper())
    for c in candidates:  # 保序：先到的（通常是主體）優先
        cn = re.sub(r"[^A-Z0-9]", "", (c or "").upper())
        if cn and cn in qn:
            return c
    return candidates[0]


def _build_spec_relation_block(db: Session, canonical_id: str) -> Optional[str]:
    """確定性「規範關係」區塊：版本家族 + 引用(outgoing) + 被引用(incoming)，直接查 KG。
    避免 LLM 自由合成把現有的邊丟掉（觀察到 310↔210、X 被誰引用、版本鏈會漏）。"""
    from .. import models
    ent = db.query(models.KGEntity).filter_by(canonical_id=canonical_id).first()
    if not ent or ent.type in ("section", "method", "annex", "document"):
        return None
    cid = ent.canonical_id
    lines: List[str] = []

    # 1) 版本家族：同基底 + 單一版本字母（810 / 810B…810H；210 / 210A…210C）
    base = re.sub(r"(?<=\d)[A-Z]$", "", cid)            # 去掉緊接數字的尾端版本字母
    fam_pat = re.compile("^" + re.escape(base) + r"[A-Z]?$")
    fam = sorted({e.canonical_id for e in db.query(models.KGEntity)
                  .filter(models.KGEntity.canonical_id.like(base + "%")).all()
                  if fam_pat.match(e.canonical_id or "")})
    if len(fam) > 1:
        lines.append(f"**版本家族**（共 {len(fam)} 版）：{' → '.join(fam)} ，最新為 **{fam[-1]}**。")

    def _readable(e) -> str:
        c = e.canonical_id or ""
        if e.type in ("section", "method", "annex") and c.startswith("doc:"):
            did = c.split("doc:", 1)[1].split("#", 1)[0]
            key = c.split("#", 1)[1] if "#" in c else ""
            doc = db.query(models.Document).filter_by(id=did).first()
            return f"{(doc.title if doc else did)} §{key}"
        return c

    # 2) outgoing：本規範引用了哪些其他標準
    out_specs: set = set()
    for r in db.query(models.KGRelation).filter_by(src_id=ent.id, rel_type="references").all():
        t = db.query(models.KGEntity).filter_by(id=r.dst_id).first()
        if t and t.type not in ("section", "method", "annex", "document") and t.canonical_id != cid:
            out_specs.add(t.canonical_id)
    if out_specs:
        lines.append(f"**引用的其他標準**（{len(out_specs)} 項）：{'、'.join(sorted(out_specs))}")

    # 3) incoming：哪些文件/章節引用了本規範（過濾自家文件的自我引用）
    in_src: set = set()
    for r in db.query(models.KGRelation).filter_by(dst_id=ent.id, rel_type="references").all():
        s = db.query(models.KGEntity).filter_by(id=r.src_id).first()
        if not s or s.canonical_id == cid:
            continue
        rd = _readable(s)
        # 自我引用：來源章節所屬文件的標題基底 == 本規範基底 → 略過
        if rd.startswith(f"{base}") or (s.type == "document" and (s.canonical_id or "").startswith("doc:")):
            continue
        in_src.add(rd)
    if in_src:
        lines.append(f"**被引用於**（{len(in_src)} 處）：{'、'.join(sorted(in_src)[:12])}")

    # 4) 版本取代：直接由版本家族排序推導（新版取代舊版），不用 KG 的 supersedes 邊
    #    —— 那些邊偶有 LLM 分類雜訊（方向相反、跨家族如 810H→167）。家族排序永遠正確。
    if len(fam) > 1:
        lines.append(f"**版本取代**：最新版 **{fam[-1]}** 取代先前版本（{' / '.join(fam[:-1])}）")

    if not lines:
        return None
    return f"**{cid}** 的規範關係（依知識圖譜，非向量檢索）：\n\n" + "\n".join(f"- {ln}" for ln in lines)


_OVERVIEW_RE = re.compile(
    r"(介紹|說明|概覽|概述|描述|重點|摘要|整理|內容|overview|brief|describe|summar|"
    r"tell me about|rundown|walk me through|各[項個種子]|每[項個種]|each\b|"
    r"all (the )?(tests|items|sub))",
    re.IGNORECASE,
)


def _looks_like_overview(q: str) -> bool:
    """判斷是否為『類別/概覽』題（介紹/說明/各項/each…）。實際是否接管仍需 KG 命中有子項的父節點。"""
    return bool(_OVERVIEW_RE.search(q or ""))


_ASPECT_RE = [
    ("criteria", re.compile(r"(判定標準|判定基準|驗收標準|合格標準|測試標準|criteria|criterion)", re.IGNORECASE)),
    ("specification", re.compile(r"(測試規格|規格|測試條件|specification|\bspec\b)", re.IGNORECASE)),
    ("objective", re.compile(r"(測試目的|目的|用途|objective)", re.IGNORECASE)),
]


def _detect_aspect(q: str) -> Optional[str]:
    """偵測『細節橫跨多項目』問題的面向：判定標準 / 規格 / 目的。"""
    for aspect, rx in _ASPECT_RE:
        if rx.search(q or ""):
            return aspect
    return None


def _group_summary(db: Session, matched: str, conversation_history) -> str:
    """對一組測試做 2~4 句摘要（自行檢索內容），供列舉題附加說明。"""
    try:
        rs = agent_tools.run_tool(db, "rag_search", {"query": matched, "top_k": 5})
        ev = rs.get("results", []) if isinstance(rs, dict) else []
        if not ev:
            return ""
        sq = (
            f"請用 2~4 句話摘要說明「{matched}」這組測試整體在測試什麼、目的為何，"
            "以段落呈現；不要再列出子項目清單。"
        )
        ans, _s, _u, _t = _grounded_synthesis(db, sq, ev, [], conversation_history,
                                              ensure_values=False)
        return ans.strip() if ans else ""
    except Exception as e:
        logger.warning("group summary failed: %s", e)
        return ""


_STRUCT_SNIPPET_CAP = 1400  # 每個子項餵進合成的內容上限（夠長到容納像 12.1 那種多段規格）


def _cap_clean(text: str, cap: int) -> str:
    """截斷到 cap 字內，但切在換行/句界，避免把內容切成半字（如「each 3 c」）。"""
    if not text or len(text) <= cap:
        return text
    cut = text[:cap]
    for sep in ("\n", "。", "; ", "；", ". "):
        i = cut.rfind(sep)
        if i > cap * 0.6:
            return cut[: i + len(sep)].rstrip()
    return cut.rstrip()


# 結構路徑在「對象未被點名」時允許的子項數上限。810H 這種整份文件節點有 30 個方法，
# 單一 Method 節點通常只有個位數程序 —— 用這個差距分辨「解析到容器」與「解析到方法」。
_STRUCT_UNNAMED_ITEM_LIMIT = 8


def _structural_evidence(db: Session, question: str, conversation_history) -> Optional[Dict[str, Any]]:
    """結構性問題（判定標準/規格/目的/列舉/概覽）→ 用 KG 把「全部子項的內容」撈齊，
    回傳 {rag_evidence, kg_notes, matched} 供 `_grounded_synthesis`(LLM) 合成最完整答案。

    重點:KG 在這裡的角色是「保證證據完整」(含關鍵字撈不到的子項),最終答案仍由 LLM 結合
    RAG 風格的引用紀律合成 —— 不再用套版直接回傳。對象名稱從『問題 + 最近一輪歷史』解析。
    """
    aspect = _detect_aspect(question)
    is_overview = _looks_like_overview(question)
    is_enum = _looks_like_enumeration(question)
    if not (aspect or is_overview or is_enum):
        return None

    search_text = question or ""
    if conversation_history:
        last = conversation_history[-1]
        search_text = f"{question} {last.get('question', '')} {(last.get('answer', '') or '')[:200]}"

    # 依問題面向取內容：有 aspect → 抓該面向段落；否則抓整節內容。
    if aspect:
        det = agent_tools.run_tool(db, "get_subitem_details", {"name": search_text, "aspect": aspect})
        items = det.get("items") or []
        content_key = "detail"
    else:
        det = agent_tools.run_tool(db, "coverage_check", {"name": search_text})
        items = det.get("items") or []
        content_key = "excerpt"

    matched = det.get("matched")
    if not matched or not items:
        return None

    # 對象解析落到「整份文件」而非某個方法時的防呆。
    #
    # 實測「濕度的我要測試條件」被模糊比對成整份 MIL-STD-810H，於是把 30 個方法的
    # 節錄全灌進脈絡，真正的濕度內容被稀釋，合成結果反而是「查無相關資料」。
    # 判準與列舉題一致：看使用者有沒有點名這個容器。沒點名、子項又多、且不是
    # 概覽/列舉題 → 視為解析錯誤，讓它走一般 RAG（那條路反而找得到正確段落）。
    if (not is_overview and not is_enum
            and len(items) >= _STRUCT_UNNAMED_ITEM_LIMIT
            and str(matched).lower() not in (question or "").lower()):
        logger.debug("structural evidence 放棄：對象「%s」未被點名且子項達 %d 個",
                     matched, len(items))
        return None

    rag_evidence: List[Dict[str, Any]] = []
    all_names: List[str] = []
    for it in items:
        num, nm = it.get("number"), it.get("name")
        label = f"{(str(num) + ' ') if num else ''}{nm}".strip()
        if nm:
            all_names.append(label)
        content = (it.get(content_key) or "").strip()
        if not content or not it.get("document_id"):
            continue
        rag_evidence.append({
            "document_id": it["document_id"],
            "title": label or nm,
            "page": it.get("page"),
            "snippet": _cap_clean(content, _STRUCT_SNIPPET_CAP),
            "score": None,
        })

    if not rag_evidence:
        return None

    # 權威完整清單當作 KG note，明確要求 LLM「全部涵蓋」(即使某些子項內容被預算截短也不可遺漏)。
    kg_notes: List[str] = []
    if all_names:
        kg_notes.append(
            f"「{matched}」在知識圖譜結構下共有 {len(all_names)} 個子項目，"
            f"回答必須完整涵蓋每一個：{'、'.join(all_names)}"
        )
    refs = det.get("references") or []
    if refs:
        kg_notes.append(f"「{matched}」引用的標準：{'、'.join(refs)}")

    return {"rag_evidence": rag_evidence, "kg_notes": kg_notes, "matched": matched}


def _seed_evidence_via_rag(db: Session, question: str, top_k: int = 5) -> tuple:
    """用「使用者原始問題」跑與 /rag/query 完全相同的檢索，回傳 (evidence, confidence)。

    走 services.retrieval（檢索邏輯的唯一實作），讓 Agent 的基準證據與 RAG 模式逐字一致
    （已驗證對地端與雲端都能撈到正確的表格段）。
    confidence = 最相關段落的 cross-encoder 分數（None 表示 CE 不可用）。
    """
    from . import rerank, retrieval

    embeddings = ai.embed_texts([question])
    if not embeddings:
        return [], None
    filtered = retrieval.hybrid_retrieve(db, question, embeddings[0], top_k)
    if not filtered:
        return [], None
    confidence = rerank.top_relevance(question, [c for c, _ in filtered])
    keep_ids = retrieval.context_keep_ids(filtered)
    evidence: List[Dict[str, Any]] = []
    ctx_used = 0
    for chunk, score in filtered:
        if chunk.id not in keep_ids:
            continue
        text = retrieval.context_text_budgeted(db, chunk, ctx_used)
        ctx_used += len(text)
        evidence.append({
            "document_id": chunk.document_id,
            "title": chunk.document.title if chunk.document else "",
            "page": chunk.page,
            "score": score,
            "snippet": text,
        })
    return evidence, confidence


def _build_enumeration_answer(db: Session, chosen: Dict[str, Any], rag_evidence, conversation_history):
    """用 KG 的權威完整子項清單組裝「確定性列舉答案」(body, sources)，不經會漏項的 grounded 合成。

    grounded 合成受 num_ctx 預算限制，6 個子項會被截到只展開前 2 個；列舉題要的是「完整列出」，
    所以直接照 KG 結構列全部，再附一段整體摘要說明（摘要才用合成）。
    """
    lines = [f"「{chosen['matched']}」共有 {len(chosen['subitems'])} 個子項目："]
    enum_sources: List[Dict[str, Any]] = []
    for it in chosen["subitems"]:
        num = it.get("number")
        lines.append(f"- {(str(num) + ' ') if num else ''}{it.get('name', '')}")
        if it.get("document_id"):
            enum_sources.append({
                "document_id": it.get("document_id"),
                "title": it.get("name", ""),
                "page": it.get("page"),
                "snippet": f"{(str(num) + ' ') if num else ''}{it.get('name', '')}",
                "score": None,
            })
    if chosen.get("references"):
        lines.append(f"\n引用標準：{'、'.join(chosen['references'])}")
    lines.append("\n（以上為知識圖譜結構的完整列舉）")
    body = "\n".join(lines)

    # 不只列清單：再用檢索內容對這組測試做摘要說明（這段才用合成，漏不漏項不影響清單完整性）。
    evidence_for_summary = list(rag_evidence or [])
    if not evidence_for_summary:
        try:
            rs = agent_tools.run_tool(db, "rag_search", {"query": chosen["matched"], "top_k": 5})
            if isinstance(rs, dict):
                evidence_for_summary = rs.get("results", []) or []
        except Exception:
            evidence_for_summary = []
    if evidence_for_summary:
        try:
            sq = (
                f"請用 2~4 句話摘要說明「{chosen['matched']}」這組測試整體在測試什麼、目的為何，"
                "以段落呈現；不要再列出子項目清單。"
            )
            s_ans, _s, _u, _t = _grounded_synthesis(db, sq, evidence_for_summary, [],
                                                    conversation_history, ensure_values=False)
            if s_ans and s_ans.strip():
                body += f"\n\n📝 摘要說明：\n{s_ans.strip()}"
        except Exception as e:
            logger.warning("enumeration summary failed: %s", e)
    return body, enum_sources


def run_agent(
    db: Session,
    question: str,
    *,
    conversation_history: Optional[List[Dict[str, Any]]] = None,
    max_steps: int = 8,
) -> Generator[Dict[str, Any], None, None]:
    """
    Yield events:
      {"type": "thought", "step": n, "text": ...}
      {"type": "tool_call", "step": n, "tool": str, "input": dict}
      {"type": "observation", "step": n, "tool": str, "output": dict}
      {"type": "final", "text": str}
      {"type": "error", "message": str}
    """
    # 問題沒指名對象卻在要具體數值 → 先反問，不要硬答（agent 的互動價值）。
    #
    # 放在最前面而非流程末端的原因:語料有 20 幾種測試方法，各有自己的條件，
    # 「找出測試條件」本質上無解。實測讓它跑下去會出現兩種壞結果：
    #   (a) 隨機挑一項測試（曾挑到彈道衝擊）回答得像真的 —— 使用者看不出挑錯對象
    #   (b) 走結構工具列出 30 個方法後提早 return，耗掉 9-14 步才給一份清單
    # 兩者都不如一開始就問清楚，而且省下整輪模型呼叫。
    # 對話歷史已指明對象時（例如上一輪在談振動測試）視為有主體，不重複反問。
    if _lacks_subject(question) and _wants_values(question):
        inherited = _history_subject(conversation_history)
        if inherited:
            # 主體省略型追問:繼承上一輪的對象去查，而不是反問或放棄。
            # 先前只是「跳過反問」但沒把主體帶進檢索，結果落到「查無相關資料」——兩頭落空。
            #
            # 拼接方式要讓結果是通順的查詢句。直接 f"{主體}的{原問題}" 會產生
            # 「濕度的我要測試條件」，檢索出的是完全無關的段落（實測落到頁 159）。
            # 先去掉「我要／請問」這類意圖前綴，再依剩下的字決定要不要加「的」。
            # 實測各種拼法的檢索品質（top-6 落在 Method 507.6 頁碼區間、且帶表號的比例）：
            #   「濕度的我要測試條件」  3/4，表號 1  ← 原本的拼法
            #   「濕度測試條件」        3/6，表號 2  ← 只去前綴反而更差
            #   「濕度測試的測試條件」  3/3，表號 3  ← 採用
            # 表號（Table 507.6-I）是後續補查數值的種子來源，所以「帶表號」比通順更重要。
            core = re.sub(r"^(我想|我要|請問|幫我|麻煩|想知道|找出|列出|請)+", "", question).strip()
            core = core or question
            base = inherited if inherited.endswith("測試") else f"{inherited}測試"
            question = f"{base}的{core}"
            yield {"type": "thought", "step": 0,
                   "text": f"問題未指名對象，沿用上一輪的「{inherited}」繼續查：{question}"}
        else:
            yield {"type": "thought", "step": 0,
                   "text": "問題未指名測試類型，先反問以縮小範圍（避免任意挑一項測試作答）。"}
            yield {"type": "final", "text": _clarify_scope_text(db, question), "sources": []}
            return

    # 關係題 + 問句點名某規範 → 先給 KG 的權威關係清單，不要讓後面的路徑搶走。
    #
    # 這段原本只放在流程末端，但 _structural_evidence 命中文件節點時會走 grounded 合成
    # 並提早 return（下方 749 行），到不了那裡。實測「MIL-STD-810H 引用了哪些標準」
    # 因此只回 RAG 合成的 2 項，而 KG 裡實際有 63 項 —— 而且同一題有時給 63 項、
    # 有時給 2 項，取決於哪條路徑先觸發。
    # 對關係題而言，KG 的完整邊列表本來就比 RAG 合成可靠（RAG 受 num_ctx 限制會漏項），
    # 所以在這裡先攔下來。應用題（「我的設備該做哪些…」）只是提到規範，不走這條。
    if _RELATION_RE.search(question or "") and not _APP_RE.search(question or ""):
        _m = _SPEC_ID_RE.search(question or "")
        if _m:
            from .. import models as _m_models

            _cand = re.sub(r"\s+", "", _m.group(1)).upper()
            _ent = (
                db.query(_m_models.KGEntity)
                .filter(_m_models.KGEntity.canonical_id == _cand)
                .first()
            )
            if _ent and _ent.type not in ("section", "method", "annex", "document"):
                _blk = _build_spec_relation_block(db, _ent.canonical_id)
                if _blk:
                    yield {"type": "thought", "step": 0,
                           "text": f"關係題且點名「{_ent.canonical_id}」→ 直接用知識圖譜的完整關係清單"
                                   "（比 RAG 合成可靠，不受 context 預算漏項影響）。"}
                    _rag_part, _rag_src = None, []
                    try:
                        _seed, _ = _seed_evidence_via_rag(db, question, top_k=5)
                        if _seed:
                            _g, _rag_src, _n, _t = _grounded_synthesis(
                                db, question, _seed, [], conversation_history
                            )
                            if _g and _g.strip():
                                _rag_part = _g.strip()
                    except Exception as e:  # noqa: BLE001
                        logger.warning("early spec-relation supplement failed: %s", e)
                    _body = _blk if not _rag_part else (
                        f"{_blk}\n\n── 補充（文件內容檢索）──\n{_rag_part}"
                    )
                    yield {"type": "final", "text": _body, "sources": _rag_src or []}
                    return

    # 純列舉題（「有哪些子項目 / 列出全部」且非規格/判定/目的等細節面向）→ 直接用 KG 確定性完整列舉。
    # 不走下方 _structural_evidence 的 grounded 合成：合成受 num_ctx 預算限制會漏項（6 子項只展開 2 個）。
    # Audit C6：補上 relation/app guard（與迴圈後方 834/794 行一致）。
    # 否則「MIL-STD-810H 引用了哪些標準？」（關係題，因「引用」被 route 判 agent）會在此被攔下，
    # list_subitems 命中 810H 文件節點 → 回「共 N 個方法」收工，引用問題完全沒被回答；
    # 「我的設備該做哪些測試？」（應用題）也會被同一路徑劫持成方法清單。
    if (
        _looks_like_enumeration(question)
        and _detect_aspect(question) is None
        and not _RELATION_RE.search(question)
        and not _APP_RE.search(question)
    ):
        try:
            fb = agent_tools.run_tool(db, "list_subitems", {"name": question})
        except Exception as e:
            logger.warning("enumeration list_subitems failed: %s", e)
            fb = None
        if isinstance(fb, dict) and fb.get("subitems"):
            yield {"type": "thought", "step": 0,
                   "text": f"偵測到列舉題：用知識圖譜完整列出「{fb.get('matched')}」全部子項目。"}
            body, esrc = _build_enumeration_answer(
                db,
                {"matched": fb.get("matched"), "subitems": fb.get("subitems"),
                 "references": fb.get("references") or []},
                None, conversation_history,
            )
            yield {"type": "final", "text": body, "sources": esrc}
            return

    # 結構性問題（逐項細節 / 概覽）：用 KG 把「全部子項內容」撈齊(保證完整、含關鍵字漏撈的子項)，
    # 再交給 RAG grounded 合成由 LLM 產生最完整、有引用的答案 —— 不用套版直接回傳。
    try:
        struct = _structural_evidence(db, question, conversation_history)
    except Exception as e:
        logger.warning("structural evidence gather failed: %s", e)
        struct = None
    if struct:
        yield {"type": "thought", "step": 0,
               "text": f"偵測到結構性問題：用知識圖譜撈齊「{struct.get('matched')}」全部子項，再結合 RAG 合成完整答案。"}
        try:
            ans, sources, _u, _t = _grounded_synthesis(
                db, question, struct["rag_evidence"], struct["kg_notes"], conversation_history
            )
        except Exception as e:
            logger.warning("structural grounded synthesis failed: %s", e)
            ans, sources = None, []
        if ans and ans.strip():
            yield {"type": "final", "text": ans.strip(), "sources": sources or []}
            return
        # 合成失敗 → 落回下方完整 ReAct loop

    llm = get_llm_provider()
    tools_desc = agent_tools.render_tools_for_prompt()
    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(tools=tools_desc, max_steps=max_steps)

    scratchpad: List[Dict[str, Any]] = []  # alternating assistant (tool call) / user (observation)
    history_messages = _build_history_messages(conversation_history or [])

    final_text: Optional[str] = None
    # Phase 0：蒐集證據供最後 grounded 合成（rag_search 命中段落 + KG/spec 關聯）
    rag_evidence: List[Dict[str, Any]] = []
    kg_notes: List[str] = []
    section_lookup_obs: Optional[Dict[str, Any]] = None  # 權威的 method/section 引用清單（確定性附加）
    spec_candidates: List[str] = []  # 過程中查到的規範 canonical_id（最後挑「問題提到的那個」當主體）
    kg_edges_seen = 0  # Phase 3：圖譜關聯數，供完整度反問
    structural_results: List[Dict[str, Any]] = []  # list_subitems 的權威完整清單（列舉題確定性作答）
    seeded: List[Dict[str, Any]] = []   # 保留供後段「補過仍無證據」的低信心兜底
    seed_conf: Optional[float] = None
    threshold = getattr(settings, "RAG_LOWCONF_CE_THRESHOLD", 0.15)

    # 雲地相容 seed：先用「使用者原始問題」做一次確定性檢索，把命中段落放進 rag_evidence。
    # 走 /rag/query 完全相同的檢索，保證最終 grounded 合成握有「對的證據」。
    # 注意：此處「不」因低信心就短路結束——改由後段「合成 → 不足則自動補充一輪 → 仍不足才低信心」
    # 處理，避免在 ReAct 工具迴圈跑之前就放棄 KG/結構工具能回答的題型（如版本/取代/引用類）。
    try:
        seeded, seed_conf = _seed_evidence_via_rag(db, question, top_k=5)
        rag_evidence.extend(seeded)
        if seeded:
            yield {"type": "thought", "step": 0,
                   "text": "先以原始問題做一次基準檢索（與 RAG 同一條 pipeline），確保證據齊全且與所用模型無關。"}
    except Exception as e:
        logger.warning("agent seed retrieval failed: %s", e)

    for step in range(1, max_steps + 1):
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            *history_messages,
            {"role": "user", "content": question},
        ]
        messages.extend(scratchpad)

        # Strong nudge if we're approaching the cap
        if step == max_steps:
            messages.append({
                "role": "user",
                "content": "You are at the final step. Emit final_answer JSON now.",
            })

        try:
            raw = llm.chat(messages, format="json")
        except TypeError:
            raw = llm.chat(messages)
        except Exception as e:
            logger.error("agent llm chat failed at step %d: %s", step, e)
            yield {"type": "error", "message": f"LLM 呼叫失敗：{e}"}
            return

        parsed = _parse_step(raw)
        if parsed is None:
            # one retry with stricter wording
            retry_msgs = messages + [{
                "role": "user",
                "content": (
                    "Your previous output was not valid JSON. Output a single JSON "
                    "object now matching either {thought, action, action_input} or "
                    "{thought, final_answer}. No prose, no fences."
                ),
            }]
            try:
                raw2 = llm.chat(retry_msgs, format="json")
            except TypeError:
                raw2 = llm.chat(retry_msgs)
            except Exception as e:
                yield {"type": "error", "message": f"LLM 重試失敗：{e}"}
                return
            parsed = _parse_step(raw2)
            if parsed is None:
                logger.warning("agent step %d gave no parseable JSON; using raw text", step)
                final_text = (raw or raw2 or "").strip() or "暫無足夠資訊"
                break

        thought = str(parsed.get("thought") or "").strip()
        if thought:
            yield {"type": "thought", "step": step, "text": thought}

        if "final_answer" in parsed:
            final_text = str(parsed.get("final_answer") or "").strip()
            break

        action = str(parsed.get("action") or "").strip()
        action_input = parsed.get("action_input") or {}
        if not isinstance(action_input, dict):
            action_input = {"raw": str(action_input)}

        # 模型有時把結束動作寫成 action 而非頂層 final_answer 鍵，例如
        #   {"thought": "...", "action": "final_answer", "action_input": {"answer": "..."}}
        # 上面的 `"final_answer" in parsed` 抓不到，就會落到 run_tool 去呼叫一個
        # 不存在的工具，觀察值變成「'final_answer' 不是有效的工具」，白耗一步。
        if action.lower() in ("final_answer", "finalanswer", "final", "answer"):
            cand = (
                action_input.get("answer")
                or action_input.get("final_answer")
                or action_input.get("text")
                or action_input.get("raw")
                or thought
            )
            final_text = str(cand or "").strip() or "暫無足夠資訊"
            break

        # 容錯：LLM 偶爾把工具名叫錯（get_sumerules_item_details…）→ 正規化成有效名，
        # 讓 SSE 事件、run_tool 分派、以及下方依 action 名的後處理都用同一個正確名稱。
        if action and action not in agent_tools.TOOLS:
            action = agent_tools.resolve_tool_name(action) or action

        if not action:
            # missing action AND no final_answer — terminate with whatever we have
            final_text = thought or "暫無足夠資訊"
            break

        yield {"type": "tool_call", "step": step, "tool": action, "input": action_input}

        observation = agent_tools.run_tool(db, action, action_input)
        yield {"type": "observation", "step": step, "tool": action, "output": observation}

        # Phase 0：累積證據供最後 grounded 合成
        if isinstance(observation, dict):
            if action == "rag_search":
                for res in observation.get("results", []):
                    if isinstance(res, dict):
                        rag_evidence.append(res)
            elif action == "list_subitems" and "error" not in observation:
                # 把「完整子項目清單」當成權威證據餵進合成，避免合成只用 RAG 片段而漏項。
                items = observation.get("subitems") or []
                names = "、".join(i.get("name", "") for i in items if i.get("name"))
                cnt = observation.get("subitem_count", len(items))
                if items:
                    structural_results.append({
                        "matched": observation.get("matched"),
                        "subitems": items,
                        "references": observation.get("references") or [],
                    })
                if names:
                    kg_notes.append(f"「{observation.get('matched')}」的完整子項目（共 {cnt} 項，來自知識圖譜結構）：{names}")
                refs = observation.get("references") or []
                if refs:
                    kg_notes.append(f"「{observation.get('matched')}」引用的標準：{'、'.join(refs)}")
            elif action in ("spec_references", "spec_supersedes_chain", "spec_lookup", "section_lookup", "document_get"):
                if "error" not in observation:
                    kg_notes.append(f"{action} → " + json.dumps(observation, ensure_ascii=False)[:900])
                    if action == "spec_references":
                        kg_edges_seen += len(observation.get("outgoing", [])) + len(observation.get("incoming", []))
                        if observation.get("center"):
                            spec_candidates.append(observation["center"])
                    if action == "spec_lookup":
                        for res in (observation.get("results") or []):
                            if isinstance(res, dict) and res.get("type") not in (
                                    "section", "method", "annex", "document") and res.get("canonical_id"):
                                spec_candidates.append(res["canonical_id"]); break
                    if action == "spec_supersedes_chain":
                        for it in (observation.get("chain") or []):
                            if it.get("is_center") and it.get("canonical_id"):
                                spec_candidates.append(it["canonical_id"]); break
                    if action == "section_lookup" and observation.get("referenced_standards"):
                        section_lookup_obs = observation  # 權威清單，稍後確定性附加進答案

        # Append to scratchpad so the LLM sees its previous tool call + result
        scratchpad.append({
            "role": "assistant",
            "content": json.dumps(parsed, ensure_ascii=False),
        })
        scratchpad.append({
            "role": "user",
            "content": (
                f"Observation from {action}:\n"
                + json.dumps(observation, ensure_ascii=False)[:6000]
            ),
        })

    if final_text is None:
        final_text = "已達最大步數，未能整合出最終答案。建議縮小問題範圍後重試。"

    # 應用題（「我的設備該做哪些測試」）：跳過所有「單一實體確定性區塊」（程序/列舉/section/spec），
    # 直接走 RAG 合成 —— 那種問題要綜合多個方法與規範，硬套單一方法的結構清單會答非所問。
    is_app = bool(_APP_RE.search(question or ""))

    # 程序列舉題（「有哪些程序 / what procedures」）：Procedure 不是 KG 節點，從方法內文掃出
    # Procedure I/II/…，優先於章節列舉/section_lookup（否則會回章節結構或引用清單而非程序）。
    if not is_app and _PROC_Q_RE.search(question or ""):
        pr = agent_tools.run_tool(db, "list_procedures", {"name": question})
        if isinstance(pr, dict) and pr.get("procedures"):
            head = pr.get("name") or pr.get("matched")
            plines = [f"**{head}** 的測試程序（共 {pr.get('count')} 個，掃自方法內文）：", ""]
            for p in pr["procedures"]:
                t = p.get("title")
                plines.append(f"- **Procedure {p['procedure']}**" + (f" — {t}" if t else ""))
            block = "\n".join(plines)
            rag_part, p_sources = None, []
            try:
                g, p_sources, _n, _t = _grounded_synthesis(db, question, rag_evidence, kg_notes, conversation_history)
                if g and g.strip():
                    rag_part = g.strip()
            except Exception as e:
                logger.warning("procedure block synthesis failed: %s", e)
            body = block if not rag_part else f"{block}\n\n── 補充（文件內容檢索）──\n{rag_part}"
            src = p_sources or [
                {"document_id": ev.get("document_id"), "title": ev.get("title"),
                 "page": ev.get("page"), "snippet": ev.get("snippet"), "score": ev.get("score")}
                for ev in seeded[:3]]
            yield {"type": "final", "text": body, "sources": src}
            return

    # 列舉題：用 list_subitems 的權威完整清單作答（不經會漏項的 RAG 合成）。
    #
    # 前提是「使用者真的在要清單」。實測「濕度測試方法」被回了全部 30 個 Method 的列舉:
    # LLM 探索途中順手呼叫了一次 list_subitems，下面的單一結果捷徑就無條件把它當成答案，
    # 真正的內容被擠到附註。判斷列舉與否要看問題本身，不能看 LLM 剛好用了哪個工具。
    #
    # 「引用/取代/被…引用」這類關係題不算列舉（否則「MIL-DTL-901 被哪些 810H 方法引用」會被
    # 誤判成「列 810H 的方法」）—— 交給後面的 section_lookup / 規範關係區塊處理。
    is_enum_q = bool(_looks_like_enumeration(question)) and not _RELATION_RE.search(question)
    ql = question.lower()
    chosen = None
    if is_enum_q:
        for sr in structural_results:
            if (sr.get("matched") or "").lower() in ql and sr.get("subitems"):
                chosen = sr
                break
        if chosen is None and len(structural_results) == 1 and structural_results[0].get("subitems"):
            chosen = structural_results[0]
    # 確定性 fallback：即使這一輪 LLM 沒呼叫 list_subitems，列舉題仍直接從問題字串解析實體並完整列舉。
    if chosen is None and is_enum_q:
        fb = agent_tools.run_tool(db, "list_subitems", {"name": question})
        if isinstance(fb, dict) and fb.get("subitems"):
            chosen = {
                "matched": fb.get("matched"),
                "subitems": fb.get("subitems"),
                "references": fb.get("references") or [],
            }

    if chosen and not is_app:
        body, enum_sources = _build_enumeration_answer(db, chosen, rag_evidence, conversation_history)
        yield {"type": "final", "text": body, "sources": enum_sources}
        return

    # 確定性：問句若明確點名某方法號（如「509.7 引用了哪些規範」），直接用該號做 section_lookup，
    # 不信任 LLM 可能挑錯的方法（觀察到 509.7 的問題被 agent 解析成 510.7）。
    qm = re.search(r"\b(\d{3}\.\d{1,2})\b", question)
    if not is_app and qm and _RELATION_RE.search(question):
        det = agent_tools.run_tool(db, "section_lookup", {"name": qm.group(1)})
        if isinstance(det, dict) and det.get("referenced_standards"):
            section_lookup_obs = det

    # KG ⊇ RAG：section_lookup 命中 → 答案 = 知識圖譜的權威外部規範清單（結構化、純 RAG 給不了）
    #          ＋ RAG 合成的內容（內部章節/程序性引用等）。Agent 模式作為 RAG 的超集，故兩者都納入。
    # KG 段以確定性方式產出（不靠 gemma4 合成，否則會被 RAG 片段蓋過而漏掉外部規範）。
    if not is_app and section_lookup_obs and section_lookup_obs.get("referenced_standards"):
        nm = section_lookup_obs.get("name") or section_lookup_obs.get("matched")
        cite: Dict[str, set] = {}
        for item in (section_lookup_obs.get("by_section") or []):
            std = str(item.get("references") or "")
            sec = str(item.get("section") or "").strip()
            if not std or std.startswith("doc:") or std == "MIL-STD-810H":
                continue
            cite.setdefault(std, set())
            if sec:
                cite[std].add(sec)
        stds = sorted(cite.keys())
        if stds:
            out = [f"**{nm}** 全章節引用的外部規範，共 {len(stds)} 項（依知識圖譜的章節引用結構，非向量檢索）：", ""]
            for s in stds:
                secs = "、".join(sorted(cite[s])[:4])
                out.append(f"- **{s}**" + (f"（見 {secs}）" if secs else ""))
            kg_block = "\n".join(out)
            # 再加上 RAG 合成（文件內容檢索：內部章節、程序性引用、背景）
            rag_part, rag_sources = None, []
            try:
                g, rag_sources, _n, _t = _grounded_synthesis(db, question, rag_evidence, kg_notes, conversation_history)
                if g and g.strip():
                    rag_part = g.strip()
            except Exception as e:
                logger.warning("section_lookup combined synthesis failed: %s", e)
            body = kg_block if not rag_part else f"{kg_block}\n\n── 補充（文件內容檢索）──\n{rag_part}"
            src = rag_sources or [
                {"document_id": ev.get("document_id"), "title": ev.get("title"),
                 "page": ev.get("page"), "snippet": ev.get("snippet"), "score": ev.get("score")}
                for ev in seeded[:3]]
            yield {"type": "final", "text": body, "sources": src}
            return

    # KG ⊇ RAG（規範類）：查過某規範(spec_lookup/spec_references/supersedes) → 用確定性「規範關係區塊」
    # （版本家族 + 引用 + 被引用 + 版本取代）＋ RAG 補充。避免自由合成丟掉現有的圖譜邊。
    spec_center = _pick_spec_center(spec_candidates, question)
    # 確定性 fallback：關係題且問句點名某規範，但 agent 沒查到 → 從問句抽規範 id 直接建區塊
    # （讓「MIL-DTL-901 被哪些方法引用」穩定走規範關係區塊）。
    if spec_center is None and _RELATION_RE.search(question or ""):
        from .. import models as _models
        m = _SPEC_ID_RE.search(question or "")
        if m:
            cand = re.sub(r"\s+", "", m.group(1)).upper()
            ent = db.query(_models.KGEntity).filter(_models.KGEntity.canonical_id == cand).first()
            if ent and ent.type not in ("section", "method", "annex", "document"):
                spec_center = ent.canonical_id
    # 應用題（「我的設備該做哪些…」）不走 spec 區塊（只是提到某規範，不是在問它的關係）→ 交給合成。
    #
    # 同理也要求「問題本身在問關係」：原本只要 agent 在探索途中呼叫過 spec_lookup /
    # spec_references，就會把整份規範關係區塊（版本家族 + 63 項引用清單 + 取代鏈）
    # 貼在答案最前面。實測問「振動測試的測試條件」時每次都被冠上這一大段，
    # 追問時又重複一次 —— 使用者要的是測試條件，沒有在問規範關係。
    is_relation_question = bool(_RELATION_RE.search(question or ""))
    if spec_center and not _APP_RE.search(question or "") and is_relation_question:
        spec_block = _build_spec_relation_block(db, spec_center)
        if spec_block:
            rag_part, rag_sources = None, []
            try:
                g, rag_sources, _n, _t = _grounded_synthesis(db, question, rag_evidence, kg_notes, conversation_history)
                if g and g.strip():
                    rag_part = g.strip()
            except Exception as e:
                logger.warning("spec-relation combined synthesis failed: %s", e)
            body = spec_block if not rag_part else f"{spec_block}\n\n── 補充（文件內容檢索）──\n{rag_part}"
            src = rag_sources or [
                {"document_id": ev.get("document_id"), "title": ev.get("title"),
                 "page": ev.get("page"), "snippet": ev.get("snippet"), "score": ev.get("score")}
                for ev in seeded[:3]]
            yield {"type": "final", "text": body, "sources": src}
            return

    # Phase 0/3：合成 → 充足性檢查 → 不足則自動補充一輪 → 重新合成 → 仍不足才低信心兜底。
    final_sources: List[Dict[str, Any]] = []

    def _synthesize():
        """用目前的 rag_evidence + kg_notes 跑帶引用的合成，回傳 (答案含覆蓋反問, 來源, n_used)。"""
        if not (rag_evidence or kg_notes):
            return None, [], 0
        try:
            g, srcs, n_used, n_total = _grounded_synthesis(
                db, question, rag_evidence, kg_notes, conversation_history
            )
            if g and g.strip():
                note = _coverage_note(max(0, n_total - n_used), kg_edges_seen)
                return g.strip() + (note or ""), srcs, n_used
        except Exception as e:
            logger.warning("grounded synthesis failed: %s", e)
        return None, [], 0

    synth, syn_sources, n_used = _synthesize()

    # 充足性檢查（確定性，不靠 LLM）：合成不出，或「迴圈完全沒撈到 KG 關聯且 seed 向量信心偏低」
    # → 答案只靠弱向量證據，視為不足，自動補充一輪（更廣檢索 + 規範關聯展開）後重新合成。
    # 也涵蓋「合成得出來、但內容是直接放棄」的情況：實測同一題有時第 1 步就回
    # 「查無相關資料」，再問一次卻答得出來——差別只在該次 seed 檢索撈到不相關段落。
    # 這種答案原本不算 weak（synth 不是 None、信心也可能過門檻），因此不會補查。
    gave_up = _is_no_answer(synth or "")
    weak = (
        (synth is None)
        or gave_up
        or (not kg_notes and seed_conf is not None and seed_conf < threshold)
    )
    if weak:
        try:
            yield {"type": "thought", "step": max_steps + 1,
                   "text": ("答案為「查無相關資料」，改以更廣檢索重試。"
                            if gave_up else
                            "初次證據不足，自動補充一輪（更廣檢索＋規範關聯展開）後重新作答。")}
            # (1) 更廣的檢索（提高 top_k），去重併入既有證據
            more, _ = _seed_evidence_via_rag(db, question, top_k=10)
            seen = {(e.get("document_id"), e.get("page"),
                     (e.get("snippet") or e.get("text") or "")[:48]) for e in rag_evidence}
            for e in more:
                k = (e.get("document_id"), e.get("page"),
                     (e.get("snippet") or e.get("text") or "")[:48])
                if k not in seen:
                    rag_evidence.append(e)
                    seen.add(k)
            # (2) 問題若含規範 ID → 自動 KG 展開引用關聯（「向量弱但 KG 強」題型的關鍵補充）
            from . import kg_extractor
            for sm in kg_extractor.extract_specs(question)[:4]:
                obs = agent_tools.run_tool(db, "spec_references", {"spec_id": sm.canonical_id, "hops": 1})
                if isinstance(obs, dict) and "error" not in obs:
                    kg_notes.append("spec_references → " + json.dumps(obs, ensure_ascii=False)[:900])
                    kg_edges_seen += len(obs.get("outgoing", [])) + len(obs.get("incoming", []))
            synth, syn_sources, n_used = _synthesize()
        except Exception as e:
            logger.warning("agent supplement round failed: %s", e)

    # 參數題的補救迴圈（確定性判斷，不依賴模型自我宣告）。
    #
    # 動機：規範文件的「INFORMATION REQUIRED」章節只列出參數「名稱」（salt solution pH、
    # fallout rate…），實際數值在另一節的程序裡。檢索常命中前者，模型便交出一張
    # 每列都寫「需依測試計畫指定」的表格 —— 對使用者等於什麼都沒查到。
    # 原本的充足性檢查只看「合成是否成功 / 有無 KG 邊 / seed 信心」，抓不到這種情況。
    #
    # 這裡改用可驗證的條件：問題在要數值 且 答案幾乎沒有帶單位的數字 → 視為未達成，
    # 用「證據裡出現的參數名稱」當新關鍵字再查一輪（那份清單本身就是最好的查詢種子）。
    for attempt in range(1, _PARAM_RETRY_MAX + 1):
        # 兩條觸發路徑：
        #   1. 使用者明確在要數值，而答案缺數值
        #   2. 問題沒明講要數值，但答案本身就是退化的（零數值 + 空話/重複）
        insufficient = (
            (_wants_values(question) and _lacks_values(synth))
            or _is_degenerate(synth)
        ) if synth else False
        if not insufficient:
            break
        terms = _extract_param_terms(rag_evidence, synth)
        if not terms:
            break
        yield {"type": "thought", "step": max_steps + 1 + attempt,
               "text": f"答案只列出需指定的項目、沒有實際數值，改用參數名稱重查："
                       + "、".join(terms[:4])}
        try:
            fresh: List[Dict[str, Any]] = []
            seen = {(e.get("document_id"), e.get("page"),
                     (e.get("snippet") or e.get("text") or "")[:48]) for e in rag_evidence}
            for term in terms[:4]:
                more, _ = _seed_evidence_via_rag(db, f"{term} value requirement", top_k=5)
                for e in more:
                    k = (e.get("document_id"), e.get("page"),
                         (e.get("snippet") or e.get("text") or "")[:48])
                    if k not in seen:
                        fresh.append(e)
                        seen.add(k)
            if not fresh:
                break
            # 補回來的證據要放在「最前面」，不能 append 到尾端。
            #
            # 合成受脈絡預算限制，只吃得下前面若干段（實測有 14 段被截掉）。
            # 這一輪是「為了補數值」而精準撈的，接在尾端等於必定被截掉 ——
            # 檢索明明撈到了頁 212 的 66ºC / 71ºC，最終答案卻仍然一個數值都沒有。
            # 帶數值的段落再優先於其餘補充，確保預算優先花在數值上。
            fresh.sort(key=lambda e: 0 if _VALUE_TOKEN_RE.search(
                e.get("snippet") or e.get("text") or "") else 1)
            rag_evidence[:0] = fresh
            new_synth, new_sources, new_used = _synthesize()
            if new_synth:
                synth, syn_sources, n_used = new_synth, new_sources, new_used
        except Exception as e:
            logger.warning("agent param retry round %d failed: %s", attempt, e)
            break

    if synth and synth.strip():
        final_text = synth
        final_sources = syn_sources
    elif not (rag_evidence or kg_notes):
        # 補過仍無任何證據 → 誠實低信心兜底（真正的最後手段，移到流程末端）。
        closest = [{"title": ev.get("title"), "page": ev.get("page"), "text": ev.get("snippet")}
                   for ev in seeded[:3]]
        if closest:
            final_text = ai.low_confidence_answer(question, closest)
            final_sources = [{"document_id": ev.get("document_id"), "title": ev.get("title"),
                              "page": ev.get("page"), "snippet": ev.get("snippet"),
                              "score": ev.get("score")} for ev in seeded[:5]]
    # else：有證據但合成不出 → 保留 ReAct 迴圈自己的 final_text。
    # （section_lookup 命中時已在上方以確定性優先作答並 return，這裡不再附加。）

    # 最後一道:不論放棄來自 ReAct 迴圈或合成結果，只要手上有檢索到的段落，
    # 就不要交出一句「查無相關資料」。
    #
    # 實測「振動測試的測試條件」同一題有時第 1 步就放棄、再問一次卻答得出來，
    # 差別只在該次 seed 撈到目錄頁。先前的偵測只檢查 synth，抓不到「迴圈自己放棄」
    # 以及「合成結果本身就是那句放棄」兩種情況。改以低信心方式列出最接近的段落
    # 並給查詢建議 —— 使用者至少知道系統看到了什麼、下一步該怎麼問。
    if _is_no_answer(final_text):
        pool = rag_evidence or seeded
        if pool:
            yield {"type": "thought", "step": max_steps + 4,
                   "text": "答案為查無資料，但檢索確實有命中段落 → 改以低信心方式列出最接近的內容。"}
            closest = [{"title": ev.get("title"), "page": ev.get("page"),
                        "text": ev.get("snippet")} for ev in pool[:3]]
            try:
                alt = ai.low_confidence_answer(question, closest)
                if alt and alt.strip():
                    final_text = alt.strip()
                    final_sources = [
                        {"document_id": ev.get("document_id"), "title": ev.get("title"),
                         "page": ev.get("page"), "snippet": ev.get("snippet"),
                         "score": ev.get("score")} for ev in pool[:5]
                    ]
            except Exception as e:  # noqa: BLE001
                logger.warning("final give-up rescue failed: %s", e)

    yield {"type": "final", "text": final_text, "sources": final_sources}
