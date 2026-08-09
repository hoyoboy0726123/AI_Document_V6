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

import contextvars
import json
import logging
import re
from typing import Any, Dict, Generator, List, Optional, Set, Tuple

from sqlalchemy import text as sa_text
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
# 「帶單位的數值」。單位表漏一項，該類題型就會被判成「答案沒有數值」而白跑
# 三輪補查，最後仍判定不足 —— 實測漏掉的有：
#   加速度 g / 脈衝 ms（衝擊 516.8）、力 N、質量 kg、長度 m、電壓 V、電流 A、
#   轉速 rpm、壓力 Pa/mbar/torr、pH。810H 的衝擊／振動／落下方法幾乎全用這些。
#   例：「峰值加速度 40 g，脈衝持續時間 11 ms」原本會被判為零數值。
# 單位邊界用 \b 收尾，避免 "5 games"、"20 gauge" 這類誤判。
_VALUE_TOKEN_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|°\s?[CF]|º[CF]|℃|℉|m/s2|m/s²|m/s|ft/min|g/m3|g/m³|g/ft3|ml/cm"
    r"|kPa|hPa|mbar|torr|psi|Pa|Hz|dB|rpm|lux|ppm"
    r"|mm|cm|km|inch|英寸|小時|分鐘|秒|天|hours?|minutes?|days?|ms"
    r"|(?:g|m|N|V|A|W|kg|kN)\b)"
    # pH 是前置單位（「pH 值應維持在 6.5」數字在後），與上面「數字＋單位」的
    # 形式相反，必須單獨列一條，否則規範裡最常見的 pH 要求會被判成沒有數值。
    r"|pH\s*值?\s*[^\d\n]{0,8}\d",
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
    r"MIL-STD|MIL-DTL|MIL-PRF|MIL-HDBK|ISO|IEC|IEEE|ASTM|Method\s*\d{3}(?:\.\d+)?|方法\s*\d{3}(?:\.\d+)?|\d{3}\.\d"
    r"|高溫|低溫|溫度衝擊|濕度|鹽霧|砂塵|沙塵|振動|震動|衝擊|加速度|太陽|輻射|黴菌|真菌"
    r"|淋雨|降雨|低壓|高度|爆炸|酸性|結冰|凍雨|運輸|噪音|彈跳|落下|傾斜"
    # 英文測試名稱。原本白名單只有中文，於是
    #     "What are the test conditions for the salt fog test?"
    # 被判成「沒有指名測試類型」而觸發反問 —— 問題明明點名了 salt fog。
    # 實測 _lacks_subject=True 且 _wants_values=True，直接走反問路徑。
    r"|salt\s*fog|humidity|vibration|shock|temperature|altitude|low\s*pressure"
    r"|sand\s*and\s*dust|blowing\s*(?:sand|dust)|fungus|mold|solar|rain|icing"
    r"|acidic|explosive|acceleration|gunfire|freeze|thaw|immersion|noise",
    re.I,
)


# 中文測試名 → 語料裡對應的英文詞。語料全是英文，中文提問時「淋雨」在文件中
# 一次都不會出現，光靠向量相似度不足以把主體帶進檢索。
#
# 為什麼需要它（實測）：問「淋雨測試的鹽溶液濃度規定是多少」，候選池前 10 名
# 全是鹽相關段落（METHOD 509.7 鹽霧 + MIL-STD-331D 第 134 頁），
# 沒有任何一塊來自 METHOD 506.6 —— 主體被完全丟棄，只剩參數在驅動檢索。
# 系統於是拿引信規範的鹽溶液比重回答淋雨測試，數值真實、來源真實、結論錯誤。
#
# 這是白名單，一定會有漏網，但用法是「有登錄才查核，沒登錄就放行」——
# 漏網只是回到現狀，不會造成新的誤擋。
_SUBJECT_TERMS = {
    # 「drip」單獨用不行 —— 鹽霧方法裡的「不得讓冷凝水滴落在試件上」也含這個字，
    # 足以讓淋雨題誤判成「證據有談淋雨」。要用完整詞組才有鑑別度。
    "淋雨": ["rain", "water spray", "drip test"], "降雨": ["rain", "water spray"],
    "鹽霧": ["salt fog", "salt spray"], "黴菌": ["fungus", "fungal"], "真菌": ["fungus", "fungal"],
    "沙塵": ["sand", "dust"], "砂塵": ["sand", "dust"],
    "濕度": ["humidity", "humid"], "高溫": ["high temperature", "hot"],
    "低溫": ["low temperature", "cold"], "溫度衝擊": ["temperature shock", "thermal shock"],
    "振動": ["vibration"], "震動": ["vibration"], "衝擊": ["shock"],
    "加速度": ["acceleration"], "太陽": ["solar"], "輻射": ["solar", "radiation"],
    "低壓": ["low pressure", "altitude"], "高度": ["altitude"],
    "爆炸": ["explosive", "explosion"], "酸性": ["acidic"],
    "結冰": ["icing", "freezing"], "凍雨": ["freezing rain"],
    "噪音": ["noise", "acoustic"], "彈跳": ["bounce"], "落下": ["drop"], "傾斜": ["inclination"],
}


def subject_terms(question: str) -> List[str]:
    """問句主體在語料中的檢索詞（含主體本身）。沒登錄的主體回空清單。"""
    subj = _find_subject(question or "")
    if not subj:
        return []
    terms = _SUBJECT_TERMS.get(subj)
    return ([subj] + terms) if terms else []


_SUBJECT_METHOD_CACHE: Dict[str, set] = {}
_SUBJECT_METHOD_MIN_CHUNKS = 8
_SUBJECT_METHOD_MIN_DENSITY = 0.5


def subject_methods(db: Session, subject: str) -> set:
    """主體對應到哪些章節（方法）。從語料密度推導，不寫死方法編號。

    作法：對每個章節，算「有多少比例的 chunk 提到該主體的英文詞」，
    比例 ≥50% 且該章節至少 8 塊的才算。實測密度非常乾淨：
        沙塵 → METHOD 510.7 94%      黴菌 → METHOD 508.8 80%
        濕度 → METHOD 507.6 90%      衝擊 → METHOD 516.8 93% / 519.8 90%
    取「集合」而非「最高的那一個」很重要 —— 振動同時屬於 514.8(85%) 與
    528.1(84%)，只取最大值會把 528.1 的正確答案誤擋掉。

    為什麼需要它：只檢查「有沒有任何一塊提到主體」擋不住 x03 —— 問沙塵測試
    要哪些黴菌菌種，回來的是 METHOD 508.8 的段落，其中偶然出現一次 dust，
    整組證據就被判定為「有談沙塵」。要看章節歸屬才擋得住。
    """
    if subject in _SUBJECT_METHOD_CACHE:
        return _SUBJECT_METHOD_CACHE[subject]
    terms = _SUBJECT_TERMS.get(subject)
    if not terms:
        _SUBJECT_METHOD_CACHE[subject] = set()
        return set()
    pats = [re.compile(r"\b" + re.escape(t) + r"\b") for t in terms]
    hit: Dict[str, int] = {}
    tot: Dict[str, int] = {}
    try:
        rows = db.execute(sa_text(
            "SELECT section_path, text FROM document_chunks WHERE section_path IS NOT NULL"
        )).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("subject_methods 查詢失敗: %s", exc)
        return set()
    for sp, txt in rows:
        base = (sp or "").split(",")[0].strip()
        tot[base] = tot.get(base, 0) + 1
        low = (txt or "").lower()
        if any(p.search(low) for p in pats):
            hit[base] = hit.get(base, 0) + 1
    found = {b for b, n in tot.items()
             if n >= _SUBJECT_METHOD_MIN_CHUNKS
             and hit.get(b, 0) / n >= _SUBJECT_METHOD_MIN_DENSITY}
    _SUBJECT_METHOD_CACHE[subject] = found
    logger.info("主體「%s」對應章節：%s", subject, sorted(found) or "（無）")
    return found


def evidence_matches_subject(question: str, evidence: List[Dict[str, Any]],
                             db: Optional[Session] = None) -> bool:
    """候選證據裡有沒有任何一塊真的在談問句指名的那個測試。

    只要有一塊命中就算通過 —— 目的是抓「一塊都沒有」的極端情況，
    那才是「主體被丟棄」的訊號。部分命中屬於正常的檢索雜訊。
    """
    terms = subject_terms(question)
    if not terms or not evidence:
        return True                      # 主體未登錄或根本沒證據 → 不查核（fail open）
    # 英文詞要用詞界比對：rain 是 drain / training / constraint 的子字串，
    # 直接 in 判斷會讓幾乎任何段落都「命中主體」，閘門等於沒有。
    pats = [re.compile(r"\b" + re.escape(t.lower()) + r"\b") if t.isascii()
            else re.compile(re.escape(t)) for t in terms]
    methods = subject_methods(db, _find_subject(question)) if db is not None else set()

    for e in evidence:
        sp = (e.get("section_path") or "").split(",")[0].strip()
        if sp:
            # 有章節標籤 → 以章節歸屬為準。標籤在但屬於別的方法，就是
            # 「內容其實在講另一項測試」，內文偶然出現一次主體詞不算數。
            if methods and sp in methods:
                return True
            if methods:
                continue
        blob = ((e.get("snippet") or e.get("text") or "") + " " + sp).lower()
        if any(p.search(blob) for p in pats):
            return True
    return False


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


# 追問裡「只是個參數名」的詞。它們同時也在 _SUBJECT_RE 的白名單裡（濕度測試、
# 溫度衝擊測試都是真的測試方法），所以單看白名單無法分辨這兩種用法：
#     「濕度測試的條件是什麼」→ 濕度是主體
#     「那濕度呢」（承接上一輪的鹽霧測試）→ 濕度是參數
_PARAM_NOUNS = {"濕度", "溫度", "高溫", "低溫", "壓力", "高度", "低壓", "時間",
                "風速", "濃度", "頻率", "加速度", "電壓", "電流", "噪音", "輻射"}
_FOLLOWUP_MAX_LEN = 18


def resolve_followup_question(question: str,
                              conversation_history: Optional[List[Dict[str, Any]]]) -> str:
    """追問若省略了主體，補上上一輪的主體後再去檢索。

    實測三種會跑題的追問（承接「鹽霧測試的箱體溫度」之後）：
        「那濕度呢」        → 被當成「濕度測試」，撈回 Method 507.6 的內容
        「這個測試要跑多久」 → 完全沒有主體，撈回振動測試的 20 小時飛行時間
        「要跑幾個循環」     → 同上
    第一種最難察覺：_find_subject 在白名單裡找到「濕度」就認定有主體了，
    而「濕度」既是參數名也是測試名。用「問句很短 + 沒有測試/試驗後綴 +
    沒有規範編號」三個條件一起判，才分得開這兩種用法。

    只在有對話歷史時作用 —— 左側面板的新問題不帶歷史，不會被改寫
    （「冷凝測試方法與條件」被誤當追問的那個舊問題就是這樣來的）。
    """
    q = (question or "").strip()
    if not q or not conversation_history:
        return question
    inherited = _history_subject(conversation_history)
    if not inherited:
        return question

    subject = _find_subject(q)
    if subject:
        # 有主體，但可能只是個參數名被誤認
        bare_param = (
            subject in _PARAM_NOUNS
            and len(q) <= _FOLLOWUP_MAX_LEN
            and not re.search(r"測試|試驗|MIL-|Method\s*\d|方法\s*\d|\d{3}\.\d", q)
        )
        if not bare_param:
            return question
    elif len(q) > _FOLLOWUP_MAX_LEN:
        # 沒主體但問得長 → 多半是獨立的新問題（「請詳細說明整份規範的剪裁流程…」），
        # 不要硬套上一輪的主體。這種情況留給底下原有的分支處理，行為不變。
        return question

    if _find_subject(str(inherited)) and inherited in q:
        return question         # 已經自己帶了同一個主體

    # 指示詞要整個拿掉，不能只吃一個字 ——「這個測試要跑多久」只去掉「這」
    # 會變成「個測試要跑多久」，拼出來的查詢句是壞的。
    core = re.sub(r"^(這個|那個|此|該|這|那|那麼|然後|接著|再來|還有|以及|and|then)\s*", "", q)
    core = re.sub(r"^(測試|試驗)(?=[要有的])", "", core)
    core = re.sub(r"[呢嗎吧？?]+$", "", core).strip() or q
    base = inherited if re.search(r"測試|試驗$", inherited) else f"{inherited}測試"
    rewritten = f"{base}的{core}"
    logger.info("追問補主體：「%s」→「%s」", q, rewritten)
    return rewritten


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
    # 關係題與列舉題的否決必須排在最前面。
    #
    # 原本先比對「條件|參數|規格」才輪到否決，於是
    #     「MIL-STD-810H 引用了哪些標準的規格」→ True
    #     「哪些方法引用了 ASTM D3951 的參數」→ True
    # 這兩題會啟動 _drop_off_topic 的證據收斂，而 in_scope 對「不在錨點文件內」
    # 的證據一律擋掉 —— 跨規範關係題的證據會被整批清空，正好毀掉它們需要的東西。
    if _RELATION_RE.search(q) or _ENUM_STRUCT_RE.search(q):
        return False
    # 明確索取數值的線索優先於其餘否決：「溫度範圍是多少」含「範圍」，
    # 但它問的是數值區間，不是文件的 scope。
    if _WANTS_VALUE_RE.search(q):
        return True
    if _PURPOSE_RE.search(q) or re.search(r"(?<![溫濕濃頻壓時速電])範圍", q):
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


# --- 給非 agent 路徑（純 RAG 端點）使用的公開介面 ---------------------------
# 這些判斷原本只服務 agent 的補救迴圈，但純 RAG 模式同樣會交出「每列都寫需依
# 測試計畫指定」的空表格，需要同一套判準。以公開名稱匯出，避免 api 層去取私有名稱。

def wants_values(question: str) -> bool:
    """問題是否在索取具體數值（條件、參數、濃度、溫度、時間…）。"""
    return _wants_values(question)


def has_values(text: str) -> bool:
    """文字中是否含帶單位的數值。"""
    return bool(_VALUE_TOKEN_RE.search(text or ""))


def value_seed_terms(evidence: List[Dict[str, Any]], answer: str = "") -> List[str]:
    """從證據中抽出「找數值用」的查詢種子（表號、循環代號、英文參數名稱）。"""
    return _extract_param_terms(evidence, answer)


# 補查數值時允許偏離原命中頁的範圍。
# 取 15 頁：實測 ±30 太寬，沙塵（命中 265-271）的範圍會涵蓋到黴菌測試（頁 240），
# 補查因此把培養溫度與礦物鹽配方當成沙塵條件撈了回來。相鄰方法的間距就是這個量級。
_SCOPE_PAGE_MARGIN = 15


def evidence_sections(evidence: List[Dict[str, Any]]) -> Dict[str, Set[str]]:
    """既有證據落在哪些章節（例如 {doc: {"METHOD 510.7"}}）。

    比 evidence_scope 的「±15 頁」精確：頁碼是位置的近似，章節才是「同一個測試方法」
    的真正邊界。一個方法可能跨 30 頁，兩個方法也可能擠在同一頁。
    只有 MIL-STD-810H 有章節標籤，其餘文件回傳空集合 → 自動退回頁碼判斷。
    """
    out: Dict[str, Set[str]] = {}
    for e in evidence:
        did, sp = e.get("document_id"), e.get("section_path")
        if did and sp:
            out.setdefault(did, set()).add(sp)
    return out


def _same_method(a: str, b: str) -> bool:
    """"METHOD 504.3" 與 "METHOD 504.3, ANNEX A" 視為同一個方法。

    ANNEX 是方法本體的附錄（504.3 的 ANNEX A 就是它的燃料清單），
    查方法本體時撈到自己的附錄是對的，撈到別的方法才是污染。
    """
    return a.split(",")[0].strip() == b.split(",")[0].strip()


def evidence_scope(pages_by_doc: List[Tuple[Optional[str], Optional[int]]]) -> Dict[str, Tuple[int, int]]:
    """由既有證據算出「同一主題的合理頁碼範圍」，供補查結果過濾。

    沒有這道約束的後果（實測）：查沙塵測試時，補查用的關鍵字
    「Test temperature(s)」把黴菌測試（Method 508，頁 240）的培養溫度與
    礦物鹽配方撈了回來，答案言之鑿鑿地把它當成沙塵測試條件。
    對照組是通用容差章節（頁 19/26）也被當成沙塵的溫度要求。

    有數值但取自錯誤的方法，比沒有數值危險得多 —— 使用者要拿這些值寫測試計畫。
    """
    scope: Dict[str, Tuple[int, int]] = {}
    for doc_id, page in pages_by_doc:
        if not doc_id or not page:
            continue
        lo, hi = scope.get(doc_id, (page, page))
        scope[doc_id] = (min(lo, page), max(hi, page))
    return {d: (lo - _SCOPE_PAGE_MARGIN, hi + _SCOPE_PAGE_MARGIN) for d, (lo, hi) in scope.items()}


def in_scope(scope: Dict[str, Tuple[int, int]], doc_id: Optional[str], page: Optional[int],
             sections: Optional[Dict[str, Set[str]]] = None,
             section_path: Optional[str] = None) -> bool:
    """補查到的段落是否落在合理範圍內。範圍未知（無頁碼）時保守放行。

    有章節標籤時以章節為準，頁碼只是後備：兩者可能不一致（METHOD 509.7 跨了
    20 頁，±15 頁會切掉它自己的後半段，卻放進隔壁 METHOD 510.7 的開頭）。
    章節資訊只有 810H 有，其餘文件仍走原本的頁碼判斷。
    """
    if not scope:
        return True
    if not doc_id or doc_id not in scope:
        return False          # 原本沒命中的文件 → 極可能是別的主題
    known = (sections or {}).get(doc_id)
    if known and section_path:
        return any(_same_method(section_path, k) for k in known)
    if page is None:
        return True
    lo, hi = scope[doc_id]
    return lo <= page <= hi


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


# 每次請求可用的「補查數值」總輪數。用 ContextVar 而非全域變數，
# 併發請求各有各的預算，不會互相扣減。
_RETRY_BUDGET_PER_REQUEST = 3


def _new_retry_budget() -> List[int]:
    """建立本次請求的補查預算（可變容器，沿呼叫鏈傳遞）。

    刻意「不」用 ContextVar：run_agent 是 generator，經 StreamingResponse 回傳時
    Starlette 的 iterate_in_threadpool 對每次 next() 都用 copy_context() 執行，
    generator 內 set() 的 ContextVar 在第一次 yield 之後就消失。實測：

        經 iterate_in_threadpool: set後=有值 → yield後=None → 之後都是 None
        同步驅動（單元測試）:      set後=有值 → yield後=有值（同一物件）

    於是每個合成點都取到 None、各自重新拿一份 3 輪額度 —— 6 個呼叫點最壞是
    24 次 LLM 生成，正是「單一請求卡住數分鐘」那個以為已修好的病。
    單元測試同步驅動 generator，所以驗不出來。
    """
    return [_RETRY_BUDGET_PER_REQUEST]


# 收斂前必須保留的證據下限。低於這個數字就不動手 —— 寧可留著雜訊，
# 也不要把脈絡砍到不足以作答。
_MIN_EVIDENCE_TO_PRUNE = 3
# 錨點頁碼的最大容許跨距。超過就代表最高分的幾筆本來就不在同一章節，
# 沒有「主題」可依循 —— 這時收斂只會砍錯。
_ANCHOR_MAX_SPREAD = 60


def _drop_off_topic(question: str, evidence: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """數值題才做：丟掉離主命中太遠的證據（多半是別的測試方法）。

    agent 自己的 rag_search 不受補查那道範圍約束，實測查沙塵測試時，
    來源裡仍會混進頁 236/240（黴菌測試 Method 508）。這一次答案沒有誤用，
    但同樣的證據送進合成，模型隨時可能把別的方法的溫度當成沙塵條件。

    只在「問題在要數值」時收斂，關係題／列舉題不受影響 —— 那些本來就需要
    跨規範查詢（「MIL-DTL-901 被哪些 810H 方法引用」），收斂會直接毀掉它們。
    """
    if not _wants_values(question) or len(evidence) < _MIN_EVIDENCE_TO_PRUNE:
        return evidence
    # 以排序最前面的幾筆當錨點（檢索分數最高＝最可能就是使用者要的章節）
    anchor = [(e.get("document_id"), e.get("page")) for e in evidence[:3]]
    scope = evidence_scope(anchor)
    # 錨點本身就分散（跨文件、或頁碼相距很遠）→ 沒有明確的主題章節可依循，
    # 推出來的範圍會大到失去意義，收斂反而可能砍錯。這種情況不動它。
    if len(scope) != 1:
        return evidence
    lo, hi = next(iter(scope.values()))
    if (hi - lo) - 2 * _SCOPE_PAGE_MARGIN > _ANCHOR_MAX_SPREAD:
        return evidence
    # 章節同樣只取錨點的（用全部證據算等於把要丟掉的離題章節也算成合法範圍）
    sections = evidence_sections(evidence[:3])
    kept = [e for e in evidence if in_scope(scope, e.get("document_id"), e.get("page"),
                                            sections, e.get("section_path"))]
    if len(kept) < _MIN_EVIDENCE_TO_PRUNE:
        return evidence      # 收斂過頭 → 放棄收斂
    if len(kept) < len(evidence):
        logger.info("數值題收斂證據：%d → %d 段（丟掉離主命中過遠的）",
                    len(evidence), len(kept))
    return kept


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
    retry_budget: Optional[List[int]] = None,
) -> tuple:
    """外層包裝：合成一次，若答案缺數值就自己補查再合成，直到夠用或試完。

    為什麼放在這裡而不是流程末端：`run_agent` 有 9 個「產出最終答案」的出口，
    其中 8 個會提早 return，繞過末端的補救迴圈。實測同一個問題在不同輪次會走到
    不同出口（LLM 每次挑的工具不同），於是「有時會自己深查、有時不會」。
    所有出口的內容答案都出自這個函式，把補救放進來才是唯一覆蓋得完的位置。

    `ensure_values=False` 供內部輔助呼叫使用（子問題拆解、摘要生成）——
    那些不是要交給使用者的最終答案，不值得多花幾輪檢索。
    """
    rag_evidence = _drop_off_topic(question, rag_evidence)
    ans, sources, n_used, n_total = _synthesize_grounded(
        db, question, rag_evidence, kg_notes, conversation_history)
    if not ensure_values:
        _ctx = [{"text": e.get("snippet") or e.get("text") or ""} for e in rag_evidence]
        return _flag_unverified_premise(
            question,
            _flag_unsourced_values(_degrade_empty_tables(ans), _ctx),
            _ctx), sources, n_used, n_total

    # 補查輪數是「整個請求」共用的預算，不是每個呼叫點各自 3 輪。
    # 單次 agent 執行會經過多個合成點，各跑各的會累積成十幾次 LLM 生成，
    # 使用者端就是長時間沒有回應。
    # 呼叫端沒帶預算（例如單元測試直接呼叫）→ 給一份獨立額度
    budget = retry_budget if retry_budget is not None else _new_retry_budget()
    evidence = list(rag_evidence)
    for _ in range(_PARAM_RETRY_MAX):
        if budget[0] <= 0:
            logger.debug("ensure_values：本次請求的補查預算已用完")
            break
        if not _needs_deeper_search(question, ans):
            break
        terms = _extract_param_terms(evidence, ans)
        if not terms:
            break
        seen = {(e.get("document_id"), e.get("page"),
                 (e.get("snippet") or e.get("text") or "")[:48]) for e in evidence}
        # 補查必須限制在「原命中所在的文件與頁碼範圍」內，否則會跨到別的測試方法：
        # 實測查沙塵測試時，關鍵字「Test temperature(s)」把黴菌測試（頁 240）的
        # 培養溫度與礦物鹽配方撈回來，答案把它當成沙塵測試條件。
        scope = evidence_scope([(e.get("document_id"), e.get("page")) for e in evidence])
        sections = evidence_sections(evidence)
        fresh: List[Dict[str, Any]] = []
        for term in terms[:4]:
            try:
                more, _ = _seed_evidence_via_rag(db, f"{term} value requirement", top_k=5)
            except Exception as e:  # noqa: BLE001
                logger.warning("ensure_values 補查「%s」失敗: %s", term, e)
                continue
            for ev in more:
                if not in_scope(scope, ev.get("document_id"), ev.get("page"),
                                sections, ev.get("section_path")):
                    continue
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
        budget[0] -= 1
        new_ans, new_sources, new_used, new_total = _synthesize_grounded(
            db, question, evidence, kg_notes, conversation_history)
        if new_ans:
            ans, sources, n_used, n_total = new_ans, new_sources, new_used, new_total
    # 補查迴圈已結束，此時才做來源查核（放在迴圈內會讓警示文字
    # 干擾 _needs_deeper_search 的判斷，見 _synthesize_grounded 的註解）。
    # 表格降級同樣要等迴圈結束：迴圈內降級會讓 _needs_deeper_search 看不到
    # 那張空表格，反而以為「答案沒有數值需求」而提早收手，少查一輪。
    _ctx = [{"text": e.get("snippet") or e.get("text") or ""} for e in rag_evidence]
    ans = _flag_unsourced_values(_degrade_empty_tables(ans), _ctx)
    ans = _flag_unverified_premise(question, ans, _ctx)
    return ans, sources, n_used, n_total


# 單位的中英對照。模型常把單位翻成中文，或把單位放進表格欄名，
# 比對時要先正規化，否則會把「有依據」的數值誤判成沒依據。
_UNIT_ZH = [("m/s", ["米/秒", "公尺/秒"]), ("mm", ["毫米", "公釐"]), ("cm", ["公分", "厘米"]),
            ("kg", ["公斤", "千克"]), ("g", ["克"]), ("hours", ["小時"]),
            ("minutes", ["分鐘"]), ("days", ["天"]), ("°c", ["攝氏"])]


def _norm_for_match(s: str) -> str:
    t = re.sub(r"\s+", "", s or "").lower().replace("º", "°").replace("³", "3").replace("²", "2")
    for canon, zhs in _UNIT_ZH:
        for z in zhs:
            t = t.replace(z, canon)
    return t


def _flag_unsourced_values(answer: Optional[str], contexts: List[Dict[str, Any]]) -> Optional[str]:
    """標出「答案裡有、但脈絡中找不到」的數值。

    為什麼需要確定性查核：
      * 分數型防線擋不住「主題無關但用詞像規範」的問題 —— 實測
        「5G 基地台的電磁曝露安全限值」CE 信心 0.655，遠高於門檻。
      * 追問時模型會整句照抄前一輪的內容，連數值一起帶過來，掛到本輪
        完全不同的來源上（實測「標準大氣條件 23±2°C、50±5% RH」掛在一個
        不含這些值的來源）。使用者回頭查證會翻不到，看起來像幻覺。
    提示詞約束對這兩種都無效（實測模型不遵守），所以改用事後比對。

    刻意「只標註、不刪改」：答案的敘述可能是對的、只是數值換算過或表格
    重排，直接刪句子會把好答案弄壞。標註讓使用者知道哪個數字要自己查證 ——
    對「拿這些值去寫測試計畫」這個用途，這比悄悄修掉更有用。
    """
    if not answer or not contexts:
        return answer
    haystack = _norm_for_match(" ".join(c.get("text") or "" for c in contexts))
    unsourced: List[str] = []
    for m in _VALUE_TOKEN_RE.finditer(answer):
        raw = m.group(0)
        core = re.match(r"^([\d.]+(?:±[\d.]+)?)", _norm_for_match(raw))
        if not core:
            continue
        if core.group(1) not in haystack:
            v = re.sub(r"\s+", " ", raw).strip()
            if v not in unsourced:
                unsourced.append(v)
    if not unsourced:
        return answer
    logger.info("答案含 %d 個脈絡中查不到的數值：%s", len(unsourced), unsourced[:5])
    return (answer.rstrip() + "\n\n⚠️ 下列數值未能在本次引用的段落中找到，請自行查證："
            + "、".join(unsourced[:8]))


# 純字母縮寫（PR / BOM / EC）與「字母+數字」的代碼／料號（22SW0 / 08G2000174J3）。
# 後者是實測補上的：教育訓練文件的內容主體就是料號，而「22SW0 是什麼類別」
# 同樣被低信心閘門擋掉 —— 原字明明就在語料裡。
_ABBREV_RE = re.compile(
    r"\b([A-Z]{2,5}|(?=[A-Z0-9-]{4,}\b)(?=[A-Z0-9-]*[A-Z])(?=[A-Z0-9-]*\d)[A-Z0-9-]+)\b")
_ABBREV_Q_MAX_LEN = 20


def literal_abbrev_hit(question: str, evidence: List[Dict[str, Any]]) -> Optional[str]:
    """短問句裡的縮寫是否原字出現在最相關的段落裡。

    cross-encoder 給不了縮寫查詢合理的分數：實測同一份教育訓練文件裡
        「PR 階段是什麼」 CE 0.091   「PR 是什麼意思」 CE 0.013
        「試產階段 PR 的定義」 CE 0.862
    三次的正解都在候選池裡（第 13 頁那張專有名詞表），但前兩種問法會被
    低信心閘門（門檻 0.15）擋掉，使用者看到「查無高度相關內容」。
    而「PR是什麼」「BOM」「EC」正是實務上最常見的問法。

    縮寫原字出現在段落裡是很強的確定性訊號，比 CE 的語意分數可靠 ——
    此時不該讓 CE 的低分否決它。限定「短問句 + 純英文大寫 2–5 字」
    是為了不影響正常的長問句（語料內不存在的題目仍由 CE 閘門把關）。
    """
    q = (question or "").strip()
    if len(q) > _ABBREV_Q_MAX_LEN:
        return None
    for m in _ABBREV_RE.finditer(q):
        tok = m.group(1)
        for e in evidence[:3]:
            if re.search(r"\b" + tok + r"\b", e.get("snippet") or e.get("text") or ""):
                return tok
    return None


def _flag_unverified_premise(question: str, answer: Optional[str],
                             contexts: List[Dict[str, Any]]) -> Optional[str]:
    """問句自己帶了數值、但檢索不到依據時，先標明前提未經證實。

    實測「鹽霧測試規定的最長連續曝露時間是 30 天，這個數字的依據是什麼」——
    系統開始替 30 天找理由，而規範裡根本沒有這個數字。假的方法編號擋得住
    （查無相關資料），假的數字擋不住，因為數字聽起來就像規範會有的東西，
    而檢索總能撈回一些「談曝露時間」的段落來支撐它。

    這是 _flag_unsourced_values 的鏡像：那支查答案裡的數值，這支查問句裡的。
    同樣只標註不改寫 —— 使用者可能是引用了別份文件的真實數字來對照。
    """
    if not question or not answer or not contexts:
        return answer
    haystack = _norm_for_match(" ".join(c.get("text") or "" for c in contexts))
    unverified: List[str] = []
    for m in _VALUE_TOKEN_RE.finditer(question):
        # 必須連單位一起比對。只比裸數字沒有意義 ——「30」在任何一份規範裡
        # 都找得到（頁碼、其他參數、表格列號），於是假前提永遠「查得到依據」。
        token = _norm_for_match(m.group(0))
        if token and token not in haystack:
            v = re.sub(r"\s+", " ", m.group(0)).strip()
            if v not in unverified:
                unverified.append(v)
    if not unverified:
        return answer
    logger.info("問句前提未獲證實：%s", unverified[:3])
    return ("⚠️ 問題中提到的「" + "」「".join(unverified[:3])
            + "」未能在檢索到的段落中找到依據，以下說明不代表規範確有此規定。\n\n"
            + answer)


_TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|[\s:\-|]+\|\s*$")
# 「這格其實沒有內容」的常見寫法。模型不會留白，它會填一個看起來很專業的佔位字。
_PLACEHOLDER_RE = re.compile(
    r"^(?:[-—–~]+|n/?a|tbd|未?(?:指定|規定|說明|提供|列出)|無|不適用|依(?:測試|試驗)?計畫(?:指定|規定)?"
    r"|見(?:測試|試驗)?計畫|由(?:採購|使用)?單位(?:指定|決定)|待定|如上|同上|\?+)$",
    re.IGNORECASE)


def _split_row(line: str) -> List[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _degrade_empty_tables(answer: Optional[str]) -> Optional[str]:
    """把「有格線、沒內容」的表格降級成條列。

    為什麼需要確定性把關：提示詞裡的「嚴禁造出沒有內容的表格」被實測反覆無視。
    模型遇到「原文只列出項目名稱、數值要由測試計畫指定」時，仍會排出一張漂亮的
    三欄表，每列的數值欄都填「依測試計畫指定」——排版看起來像查到了東西，
    使用者要逐格讀完才發現一個數字都沒有。這比直接說「原文未給值」更浪費時間。

    判定條件刻意保守，三個都成立才降級：
      1. 資料列 ≥3（1–2 列可能只是還沒填完，不足以判斷）
      2. 第一欄以外的所有格子都不含任何數值 token
      3. 那些格子去重後只剩 ≤2 種寫法，且全部是佔位字
    只要表格裡有任何一個真數值就原樣保留 —— 誤降級一張真表格的代價，
    遠高於漏掉一張空表格。
    """
    if not answer or "|" not in answer:
        return answer
    lines = answer.splitlines()
    out: List[str] = []
    i = 0
    degraded = 0
    while i < len(lines):
        if not _TABLE_LINE_RE.match(lines[i]):
            out.append(lines[i]); i += 1; continue
        j = i
        while j < len(lines) and _TABLE_LINE_RE.match(lines[j]):
            j += 1
        block = lines[i:j]
        body = [ln for ln in block[1:] if not _TABLE_SEP_RE.match(ln)]
        cells = [c for ln in body for c in _split_row(ln)[1:] if c]
        is_empty = (
            len(body) >= 3
            and cells
            and not any(_VALUE_TOKEN_RE.search(c) for c in cells)
            and len({c.lower() for c in cells}) <= 2
            and all(_PLACEHOLDER_RE.match(c) for c in cells)
        )
        if is_empty:
            names = [r[0] for r in (_split_row(ln) for ln in body) if r and r[0]]
            out.append("以下為原文列出、但數值需由測試計畫另行指定的項目：")
            out.extend(f"- {n}" for n in names)
            degraded += 1
        else:
            out.extend(block)
        i = j
    if not degraded:
        # 一張都沒降級就回原字串：splitlines/join 會吃掉尾端換行，
        # 對「什麼都沒改」的情況造成不必要的差異。
        return answer
    logger.info("降級 %d 張無內容表格為條列", degraded)
    return "\n".join(out)


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
    # 頁距要真的算出來。原本 agent 路徑一律寫 None，於是提示詞裡整整一條
    # 「標記⚠️頁距的來源極可能屬於不同測試項目…優先捨棄」的防污染規則
    # 在 agent 模式下是死的 —— 而 agent 恰恰最需要它：證據來自 seed 檢索、
    # LLM 自由 rag_search、補查、KG 子項四個來源混合，頁碼跨度遠大於純 RAG。
    primary_page = next((e.get("page") for e, _ in deduped if e.get("page")), None)
    for ev, text in deduped:
        if used + len(text) > budget:
            text = text[: max(0, budget - used)]
        if not text:
            break
        ev_page = ev.get("page")
        contexts.append({
            "source_num": len(contexts) + 1,
            "title": ev.get("title") or "",
            "page": ev_page,
            "page_gap": (abs(ev_page - primary_page)
                         if primary_page and ev_page else None),
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
    # 注意：查核**不能**放在這裡。本函式在補查迴圈內會被反覆呼叫，而查核附加的
    # 警示文字本身含有數值（「…請自行查證：72 小時」），下一輪的
    # _needs_deeper_search 會因此判定「答案已經有數值」而不再補查 ——
    # 等於警示訊息把補救機制關掉了（實測數值落地率因此掉 1 題）。
    # 查核改在 _grounded_synthesis 的迴圈結束後做一次。
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
    r"(有哪些|哪些|哪幾個|幾個|列出|列舉|清單|子項目|有什麼|包含哪些|底下有|下有|"
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
    # 「有哪些」與名詞之間必須容許空白：實測「METHOD 514.8 底下有哪些 ANNEX」
    # 因為中間那個半形空格而不匹配，route_mode 於是把它送去 RAG 分支，
    # KG 裡明明有完整正確的 ANNEX A–F 清單卻從來沒被用到。
    # 「有幾個 / 包含哪幾個」也是同樣的列舉意圖，原本完全沒涵蓋。
    r"(有|包含)\s*(哪些|幾個|哪幾個)\s*(測試|方法|程序|步驟|項目|子|method|annex|附錄|章節))",
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
    # 追問補主體。Agent 路徑早就有這道處理，這條分支沒有 —— 於是承接
    # 「鹽霧測試的箱體溫度」之後問「那濕度呢」，會撈回濕度測試的內容；
    # 問「這個測試要跑多久」會撈回振動測試的 20 小時飛行時間。
    # 混合模式路由到 RAG 分支是使用者最常走的路，這個漏洞影響面比 Agent 大。
    question = resolve_followup_question(question, conversation_history)
    seeded, _conf = _seed_evidence_via_rag(db, question, top_k=5)

    subject_caution = ""
    # 主體一致性查核：問句指名了某項測試，但候選證據裡一塊都沒談到它。
    # 這代表檢索只滿足了參數（「鹽溶液濃度」）而把主體（「淋雨測試」）丟了，
    # 接下來合成出的答案會是「別的測試的真實數值」——最難察覺的一種錯。
    if seeded and not evidence_matches_subject(question, seeded, db):
        terms = subject_terms(question)
        logger.info("主體一致性未過，改用英文詞重查：%s", terms[1:])
        retry_q = f"{' '.join(terms[1:])} {question}"
        try:
            reseeded, re_conf = _seed_evidence_via_rag(db, retry_q, top_k=5)
        except Exception as exc:  # noqa: BLE001
            logger.warning("主體重查失敗: %s", exc)
            reseeded, re_conf = [], None
        if reseeded and evidence_matches_subject(question, reseeded, db):
            seeded, _conf = reseeded, re_conf
            # 重查有救回主體，但「原始檢索一塊都沒命中主體」本身就是訊號：
            # 這項參數多半不由該測試規範。實測救回 METHOD 506.6 的內容後，
            # 模型仍用「淋雨測試的鹽溶液濃度規定如下」開頭，再把降雨流量填進去 ——
            # 換了正確的來源，卻仍在回答一個不存在的問題。
            subject_caution = (
                f"⚠️ 原始檢索結果不屬於「{_find_subject(question)}」測試，已改用該測試的內容重新檢索。"
                f"若下列內容未直接回答所問的參數，表示該測試可能未規定此項，請勿套用其他測試的數值。\n\n")
        else:
            # 重查後仍然沒有 —— 這份規範的該項測試很可能真的沒有這個參數。
            # 誠實說沒有，遠好過拿另一項測試的數值來充數。
            subj = _find_subject(question)
            logger.info("主體「%s」重查後仍無相關段落，改為誠實回覆", subj)
            return (f"可用段落中找不到「{subj}」相關測試對此問題的規定 —— "
                    f"檢索到的內容屬於其他測試項目，不能直接套用。\n\n"
                    f"建議確認該項參數是否確實由此測試規範，或改以規範編號／方法編號查詢。",
                    [])

    # 低信心兜底 —— 這道防線原本只存在於 rag.py 的 /query 端點，這裡漏了，
    # 而本函式正是「混合模式路由到 RAG 分支」時實際跑的程式碼。
    #
    # 後果是同一個離題問題，純 RAG 模式會誠實說查不到，混合模式卻編出數值：
    #     「這批文件裡量子電腦的低溫維持條件是什麼」CE 信心 0.094（門檻 0.15）
    #     → 仍答出「低溫測試條件為 -65°F (-54°C)…」，把某個規範的冷卻流體
    #       溫度安到量子電腦上。對照組 n03/n04/n05 信心更低才沒編。
    #
    # docstring 寫「與 /rag/query 同一條」卻少了這段，是最容易漏掉的那種不一致。
    # _seed_evidence_via_rag 回傳的 conf 本來就是最相關段落的 cross-encoder 分數，
    # 直接用即可（CE 不可用時為 None，照常作答，與 /query 端點一致）。
    if seeded:
        _thr = getattr(settings, "RAG_LOWCONF_CE_THRESHOLD", 0.15)
        _abbrev = literal_abbrev_hit(question, seeded)
        if _abbrev:
            logger.info("縮寫「%s」原字命中段落，略過低信心閘門（CE=%s）", _abbrev, _conf)
        if _abbrev is None and _conf is not None and _conf < _thr:
            closest = [{"title": ev.get("title"), "page": ev.get("page"),
                        "text": ev.get("snippet")} for ev in seeded[:3]]
            low_src = [{"document_id": ev.get("document_id"), "title": ev.get("title"),
                        "page": ev.get("page"), "snippet": ev.get("snippet"),
                        "score": ev.get("score")} for ev in seeded[:5]]
            logger.info("run_rag_only 低信心兜底：CE=%.3f < %.2f", _conf, _thr)
            return ai.low_confidence_answer(question, closest), low_src

    ans, sources, n_used, n_total = _grounded_synthesis(db, question, seeded, [], conversation_history,
                                                    retry_budget=_new_retry_budget())
    if ans and ans.strip():
        note = _coverage_note(max(0, n_total - n_used), 0)
        return subject_caution + ans.strip() + (note or ""), sources
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

    embeddings = ai.embed_query(question)
    if not embeddings:
        return [], None

    # 比較題（問句同時點名 2 份以上規範）必須分頭查再合併。單次檢索一定會被
    # 其中一份主導：實測「810H 的溫度量測公差與 331D 的…各是多少」候選池
    # 10 塊裡 9 塊來自 810H、0 塊來自 331D，於是系統回「查無相關資料」——
    # 它其實只查了一份，另一份根本沒進候選池，rerank 再強也救不回來。
    stripped_q, spec_docs = retrieval.resolve_spec_docs(db, question)
    if len(spec_docs) >= 2:
        per_doc = max(2, top_k // len(spec_docs))
        emb2 = ai.embed_query(stripped_q) or embeddings
        filtered = []
        for did in spec_docs:
            try:
                filtered.extend(retrieval.hybrid_retrieve(
                    db, stripped_q, emb2[0], per_doc, document_id=did))
            except Exception as exc:  # noqa: BLE001
                logger.warning("比較題分頭檢索失敗 doc=%s: %s", did[:8], exc)
        logger.info("比較題分頭檢索：%d 份規範，各取 %d 塊，共 %d 塊",
                    len(spec_docs), per_doc, len(filtered))
    else:
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
            # 章節標籤（只有 810H 有；其餘文件為 None）供 in_scope 判斷是否同一個測試方法
            "section_path": getattr(chunk, "section_path", None),
        })
    return evidence, confidence


def enumeration_target(db: Session, question: str) -> Optional[str]:
    """列舉題真正要列的對象。解析不出更具體的層級時回 None（照舊用整句去查）。

    直接把整句丟給 list_subitems 會解析到「文件」層級：
        「MIL-STD-810H 的 METHOD 514.8 底下有哪些 ANNEX」→ 回整份規範的 30 個 Method
        「黴菌測試方法底下有幾個附錄」               → 同上
    兩題使用者要的都是某一個方法的附錄。問句裡有方法編號就用它；只有測試名稱
    時用語料密度推導的對應表換算（黴菌 → METHOD 508.8）。
    """
    m = re.search(r"(?:METHOD|方法)\s*(\d{3}\.\d)", question or "", re.I)
    if m:
        return f"METHOD {m.group(1)}"
    subj = _find_subject(question or "")
    if subj and not _SPEC_ID_RE.search(question or ""):
        methods = subject_methods(db, subj)
        if len(methods) == 1:
            return next(iter(methods))
    return None


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


def _emit_final(db: Session, question: str, text: str,
                sources: Optional[List[Dict[str, Any]]],
                fallback_evidence: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """所有「產出最終答案」的出口都必須經過這裡。

    run_agent 有 9 個 yield final 的地方，先前只有末端那一個做了完整處理
    （查無資料兜底、來源正規化）。其餘 8 個提早 return，於是：
      * 結構性出口若合成出「查無相關資料」就照樣送出，而末端出口會用低信心
        兜底列出最接近的內容 —— 同一個問題走哪個出口決定了它有沒有兜底
      * 早期 spec 關係出口的 sources 是 `_rag_src or []`，沒有 seeded 兜底，
        使用者可能看到答案卻沒有任何可點的來源

    這與 _grounded_synthesis 那次搬遷要解決的是同一類問題：橫切關注點散落在
    多個出口，新增一個出口就漏一次。三份獨立審查都指出了這個結構。
    """
    txt = (text or "").strip()
    src = list(sources or [])

    # 查無資料兜底：手上有檢索到的段落就不要交出一句「查無相關資料」。
    if _is_no_answer(txt) and fallback_evidence:
        closest = [{"title": ev.get("title"), "page": ev.get("page"),
                    "text": ev.get("snippet")} for ev in fallback_evidence[:3]]
        try:
            alt = ai.low_confidence_answer(question, closest)
            if alt and alt.strip():
                txt = alt.strip()
                src = [{"document_id": ev.get("document_id"), "title": ev.get("title"),
                        "page": ev.get("page"), "snippet": ev.get("snippet"),
                        "score": ev.get("score")} for ev in fallback_evidence[:5]]
        except Exception as e:  # noqa: BLE001
            logger.warning("_emit_final 兜底失敗: %s", e)

    # 來源正規化：沒有來源但手上有證據時補上，讓前端至少有東西可點。
    if not src and fallback_evidence:
        src = [{"document_id": ev.get("document_id"), "title": ev.get("title"),
                "page": ev.get("page"), "snippet": ev.get("snippet"),
                "score": ev.get("score")} for ev in fallback_evidence[:5]]

    return {"type": "final", "text": txt, "sources": src}


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
    _budget = _new_retry_budget()   # 本次請求共用；不可用 ContextVar，見 _new_retry_budget
    # 提前宣告：_emit_final 的兜底參數需要它，而有三個早期出口在原本的宣告
    # 位置之前就會 yield（實測 NameError）。空 list 代表「還沒檢索過」，
    # 兜底自然跳過。
    seeded: List[Dict[str, Any]] = []
    # 問題沒指名對象卻在要具體數值 → 先反問，不要硬答（agent 的互動價值）。
    #
    # 放在最前面而非流程末端的原因:語料有 20 幾種測試方法，各有自己的條件，
    # 「找出測試條件」本質上無解。實測讓它跑下去會出現兩種壞結果：
    #   (a) 隨機挑一項測試（曾挑到彈道衝擊）回答得像真的 —— 使用者看不出挑錯對象
    #   (b) 走結構工具列出 30 個方法後提早 return，耗掉 9-14 步才給一份清單
    # 兩者都不如一開始就問清楚，而且省下整輪模型呼叫。
    # 對話歷史已指明對象時（例如上一輪在談振動測試）視為有主體，不重複反問。
    # 先做「參數型追問」的補主體（「那濕度呢」這種白名單誤判成有主體的情況）。
    # 底下原有的分支只處理「完全沒有主體」，而且要求 _wants_values —— 實測
    # 「這個測試要跑多久」兩個條件都不成立，於是整段跳過。
    question = resolve_followup_question(question, conversation_history)

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
            yield _emit_final(db, question, _clarify_scope_text(db, question), [])
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
                                db, question, _seed, [], conversation_history,
                                retry_budget=_budget,
                            )
                            if _g and _g.strip():
                                _rag_part = _g.strip()
                    except Exception as e:  # noqa: BLE001
                        logger.warning("early spec-relation supplement failed: %s", e)
                    _body = _blk if not _rag_part else (
                        f"{_blk}\n\n── 補充（文件內容檢索）──\n{_rag_part}"
                    )
                    yield _emit_final(db, question, _body, _rag_src, seeded)
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
            fb = agent_tools.run_tool(
                db, "list_subitems", {"name": enumeration_target(db, question) or question})
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
            yield _emit_final(db, question, body, esrc, seeded)
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
                db, question, struct["rag_evidence"], struct["kg_notes"], conversation_history,
                retry_budget=_budget,
            )
        except Exception as e:
            logger.warning("structural grounded synthesis failed: %s", e)
            ans, sources = None, []
        if ans and ans.strip():
            yield _emit_final(db, question, ans, sources, seeded)
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
                g, p_sources, _n, _t = _grounded_synthesis(db, question, rag_evidence, kg_notes, conversation_history, retry_budget=_budget)
                if g and g.strip():
                    rag_part = g.strip()
            except Exception as e:
                logger.warning("procedure block synthesis failed: %s", e)
            body = block if not rag_part else f"{block}\n\n── 補充（文件內容檢索）──\n{rag_part}"
            src = p_sources or [
                {"document_id": ev.get("document_id"), "title": ev.get("title"),
                 "page": ev.get("page"), "snippet": ev.get("snippet"), "score": ev.get("score")}
                for ev in seeded[:3]]
            yield _emit_final(db, question, body, src, seeded)
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
        # 列舉對象由「問句自己」決定，不看 LLM 這一輪剛好呼叫了什麼。
        #
        # 實測兩種都會落到文件層級、回整份規範的 30 個 Method：
        #   「MIL-STD-810H 的 METHOD 514.8 底下有哪些 ANNEX」——問句同時有文件與
        #     方法，LLM 查了文件，單一結果捷徑就把它當答案
        #   「黴菌測試方法底下有幾個附錄」——沒有方法編號，解析不到實體
        # 兩題使用者要的都是某一個方法的附錄，不是整份規範的方法清單。
        #
        # 先試「問句裡的方法編號」，再試「測試名稱換算成方法」（用語料密度
        # 推導的對應表），都不成才退回 LLM 的結果。
        target = enumeration_target(db, question)
        if target:
            tr = agent_tools.run_tool(db, "list_subitems", {"name": target})
            if isinstance(tr, dict) and tr.get("subitems"):
                logger.info("列舉題鎖定對象：%s（問句層級優先於 LLM 選擇）", target)
                chosen = {"matched": tr.get("matched"), "subitems": tr["subitems"],
                          "references": tr.get("references") or []}
        if chosen is None:
            for sr in structural_results:
                if (sr.get("matched") or "").lower() in ql and sr.get("subitems"):
                    chosen = sr
                    break
        if chosen is None and len(structural_results) == 1 and structural_results[0].get("subitems"):
            chosen = structural_results[0]
    # 確定性 fallback：即使這一輪 LLM 沒呼叫 list_subitems，列舉題仍直接從問題字串解析實體並完整列舉。
    if chosen is None and is_enum_q:
        # 問句用測試名稱指涉方法時（「黴菌測試底下有幾個附錄」），先把主體
        # 換算成方法編號再查 —— 否則 list_subitems 解析不到實體，會退回文件層級
        # 而列出整份規範的 30 個 Method。換算用的是從語料密度推導的對應表。
        lookup = question
        subj = _find_subject(question)
        if subj and not re.search(r"MIL-|METHOD\s*\d|方法\s*\d|\d{3}\.\d", question, re.I):
            methods = subject_methods(db, subj)
            if len(methods) == 1:
                lookup = next(iter(methods))
                logger.info("列舉題主體「%s」換算為 %s", subj, lookup)
        fb = agent_tools.run_tool(db, "list_subitems", {"name": lookup})
        if isinstance(fb, dict) and fb.get("subitems"):
            chosen = {
                "matched": fb.get("matched"),
                "subitems": fb.get("subitems"),
                "references": fb.get("references") or [],
            }

    if chosen and not is_app:
        body, enum_sources = _build_enumeration_answer(db, chosen, rag_evidence, conversation_history)
        yield _emit_final(db, question, body, enum_sources, seeded)
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
    # 兩道守衛 —— 與下方 spec_center 路徑一致，先前只有 section_lookup 這條漏掉。
    #
    # (1) 必須是關係題。實測問「鹽霧測試的測試條件與持續時間」時，LLM 探索途中
    #     挑錯方法（鹽霧是 509.7，它呼叫了 510.7 砂塵）就把 section_lookup_obs
    #     無條件設定，最終答案的主體變成「METHOD 510.7 全章節引用的外部規範，
    #     共 6 項」，真正的答案被降級成「── 補充（文件內容檢索）──」。
    # (2) 該方法號必須真的出現在問句裡。LLM 挑錯方法時不該由它決定答案主體。
    _sec_name = str(section_lookup_obs.get("name") or section_lookup_obs.get("matched") or "") \
        if section_lookup_obs else ""
    _sec_num = re.search(r"\d{3}\.\d{1,2}", _sec_name)
    _named_by_user = bool(_sec_num and _sec_num.group(0) in (question or ""))
    if (not is_app and section_lookup_obs and section_lookup_obs.get("referenced_standards")
            and _RELATION_RE.search(question or "") and _named_by_user):
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
                g, rag_sources, _n, _t = _grounded_synthesis(db, question, rag_evidence, kg_notes, conversation_history, retry_budget=_budget)
                if g and g.strip():
                    rag_part = g.strip()
            except Exception as e:
                logger.warning("section_lookup combined synthesis failed: %s", e)
            body = kg_block if not rag_part else f"{kg_block}\n\n── 補充（文件內容檢索）──\n{rag_part}"
            src = rag_sources or [
                {"document_id": ev.get("document_id"), "title": ev.get("title"),
                 "page": ev.get("page"), "snippet": ev.get("snippet"), "score": ev.get("score")}
                for ev in seeded[:3]]
            yield _emit_final(db, question, body, src, seeded)
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
                g, rag_sources, _n, _t = _grounded_synthesis(db, question, rag_evidence, kg_notes, conversation_history, retry_budget=_budget)
                if g and g.strip():
                    rag_part = g.strip()
            except Exception as e:
                logger.warning("spec-relation combined synthesis failed: %s", e)
            body = spec_block if not rag_part else f"{spec_block}\n\n── 補充（文件內容檢索）──\n{rag_part}"
            src = rag_sources or [
                {"document_id": ev.get("document_id"), "title": ev.get("title"),
                 "page": ev.get("page"), "snippet": ev.get("snippet"), "score": ev.get("score")}
                for ev in seeded[:3]]
            yield _emit_final(db, question, body, src, seeded)
            return

    # Phase 0/3：合成 → 充足性檢查 → 不足則自動補充一輪 → 重新合成 → 仍不足才低信心兜底。
    final_sources: List[Dict[str, Any]] = []

    def _synthesize():
        """用目前的 rag_evidence + kg_notes 跑帶引用的合成，回傳 (答案含覆蓋反問, 來源, n_used)。"""
        if not (rag_evidence or kg_notes):
            return None, [], 0
        try:
            g, srcs, n_used, n_total = _grounded_synthesis(
                db, question, rag_evidence, kg_notes, conversation_history,
                retry_budget=_budget,
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
            # 只在新結果有內容時才覆寫。
            #
            # 原本無條件覆寫，而 _synthesize 內部的 except 會把失敗吞成
            # (None, [], 0)。第三個 weak 條件（答案很好、只是 CE 信心低且沒有
            # KG note）成立時，若補充輪的合成失敗，**第一次的好答案就被丟掉**，
            # 流程落到「保留 ReAct 迴圈自己的 final_text」。
            # _grounded_synthesis 內的同類迴圈本來就有 if new_ans 保護，這裡沒有。
            _new = _synthesize()
            if _new[0]:
                synth, syn_sources, n_used = _new
        except Exception as e:
            logger.warning("agent supplement round failed: %s", e)

    # 註：原本這裡還有一個「參數題補救迴圈」，已移除。
    #
    # 該邏輯現在內建在 `_grounded_synthesis` 裡（見該函式），因為 run_agent 有 9 個
    # 產出答案的出口、8 個會提早 return，放在流程末端等於大部分情況都跑不到。
    # 但搬進去之後忘了把這裡的舊迴圈刪掉，變成雙重重試：合成本身已重試 3 輪，
    # 外層再判定一次又跑 3 輪，而 _grounded_synthesis 在單次 agent 執行中會被
    # 呼叫 4 個地方 —— 最壞情況是 16 次 LLM 生成加 12 輪檢索。
    # 實測單一請求卡住 6 分鐘、一分鐘內寫出 1031 行日誌，使用者端表現為系統無回應。

    # 檢索信心過低 → 不要硬答，即使合成得出東西。
    #
    # 這道判斷原本只在 run_rag_only（混合模式的 RAG 分支）有，Agent 模式沒有 ——
    # 同一個離題問題，混合模式誠實說查不到，Agent 模式卻編出數值：
    #     「這批文件裡量子電腦的低溫維持條件」CE 信心 0.094（門檻 0.15）
    #     → 「低溫維持條件為將溫度降至 -10 °C…」
    #
    # 下面那個 elif 只在「完全沒有證據」時兜底，但離題問題總是撈得到
    # 「最接近但不相關」的段落，所以永遠不會觸發。
    #
    # 這是今天第三次遇到同型問題：同一個防護只補了一條路徑。
    #
    # 條件刻意「不」包含 not kg_notes。第一版照抄了下方 weak 判斷的組合，結果
    # 這道閘門永遠不觸發 —— Agent 模式會主動呼叫 KG 工具，kg_notes 幾乎總是非空。
    # 兩者的目的不同：
    #   weak      判斷「要不要再補一輪檢索」→ 有 KG 線索可挖就不算 weak，合理
    #   _too_weak 判斷「該不該誠實說查不到」→ kg_notes 只代表 agent 查過規範關係，
    #             不代表答案有依據，拿它當「有把握」的證據是錯的
    _too_weak = (seed_conf is not None and seed_conf < threshold)
    if _too_weak and seeded:
        logger.info("run_agent 低信心兜底：CE=%.3f < %.2f", seed_conf, threshold)
        closest = [{"title": ev.get("title"), "page": ev.get("page"),
                    "text": ev.get("snippet")} for ev in seeded[:3]]
        final_text = ai.low_confidence_answer(question, closest)
        final_sources = [{"document_id": ev.get("document_id"), "title": ev.get("title"),
                          "page": ev.get("page"), "snippet": ev.get("snippet"),
                          "score": ev.get("score")} for ev in seeded[:5]]
    elif synth and synth.strip():
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

    yield _emit_final(db, question, final_text, final_sources, rag_evidence or seeded)
