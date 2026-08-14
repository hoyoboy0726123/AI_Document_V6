"""引用事後校驗：答案生成後，驗證每個 [來源N] 是否指向真正支持該段落的來源。

為什麼需要：7B 模型不做逐段歸屬，常挑一個編號套用到全部段落。實測
「buying mode有幾種」四個條目全標 [來源4]（第 11 頁的類別代號表），
內容其實來自來源 2（延伸到第 14 頁 BUYING MODE 投影片的切片）——
純 RAG 與混合模式行為一致。引用錯誤比答錯更難察覺：使用者點開來源
想驗證，看到不相關的內容，反而以為答案是編的。

為什麼是事後校驗而不是改提示詞：提示詞層的修補已實測兩次淨損（見
DEPLOY_NOTES）。本模組只改引用「標記」、完全不碰答案內容，且為
確定性字串比對 —— 同樣輸入永遠同樣輸出，不受模型能力限制。

保守原則（寧可不改，不可改錯）：
  - 原引用的支持度已達門檻 → 保留，即使別的來源分數更高
  - 沒有任何來源達到門檻 → 保留原樣，不猜
  - 標記編號不在來源對照表裡（如指向知識圖譜附註塊）→ 跳過不動
"""
from __future__ import annotations

import logging
import re
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

_MARKER_RE = re.compile(r"\[來源(\d+)\]")
# ASCII 詞（含 & / . + - 連接的技術詞，如 Buy、Sell、K/B、0A）與數字
_ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9&/.+\-]*")
_CJK_RUN_RE = re.compile(r"[一-鿿]+")

# 支持度門檻。claim 的詞集有這個比例出現在來源文字中，就視為「該來源支持它」。
# 0.30：正確配對實測會到 0.6+（answer 幾乎逐句取自來源），錯誤配對通常 <0.1
# （只有零星常用字重疊）。中間留了一整段緩衝。
_KEEP_MIN = 0.30
# 改標門檻：新來源自己要夠強，而且要明顯強過原引用，兩個條件都要。
_FIX_MIN = 0.40
_FIX_MARGIN = 0.15
# claim 太短沒有比對意義（例如只有「是。」）；往前串接前文湊到這個長度。
_CLAIM_TARGET_CHARS = 40
_CLAIM_MAX_CHARS = 600


def _tokens(text: str) -> set:
    """內容詞集：ASCII 詞（≥2 字元，轉小寫）+ 中文相鄰字元的 bigram。

    不取中文單字（太多常用字，噪音），bigram 才有辨識度；
    英文技術詞（Buy & Sell、EMS、Turnkey、0A）是最強的鑑別訊號。
    """
    toks: set = set()
    for m in _ASCII_TOKEN_RE.finditer(text.lower()):
        t = m.group(0).strip(".&/+-")
        if len(t) >= 2:
            toks.add(t)
    for run in _CJK_RUN_RE.findall(text):
        for i in range(len(run) - 1):
            toks.add(run[i:i + 2])
    return toks


def _support(claim_tokens: set, source_tokens: set) -> float:
    if not claim_tokens:
        return 0.0
    return len(claim_tokens & source_tokens) / len(claim_tokens)


def _strip_markers(line: str) -> str:
    return _MARKER_RE.sub("", line)


def _is_citation_only(line: str) -> bool:
    """整行只有引用標記（含前導的 - * • 與空白）。"""
    rest = _strip_markers(line)
    return bool(_MARKER_RE.search(line)) and not re.sub(r"[\s\-*•·.、，,：:（）()]+", "", rest)


_REF_HEADER_RE = re.compile(r"^\s*\**\s*參考來源")


def _is_list_item(lines: List[str], i: int) -> bool:
    """僅含標記的行是「參考來源清單列」還是「內容引用」？

    表格下常有單獨一行 [來源N] 指涉上方整張表 —— 那是內容引用，要校驗；
    文末「參考來源：」底下的「* [來源N]」才是清單列。用前一個非空行判別：
    是「參考來源」標頭或另一個引用列 → 清單；否則（表格列、內文）→ 內容。
    """
    if not _is_citation_only(lines[i]):
        return False
    j = i - 1
    while j >= 0 and not lines[j].strip():
        j -= 1
    if j < 0:
        return False
    prev = lines[j]
    return bool(_REF_HEADER_RE.search(prev)) or _is_citation_only(prev)


def validate_citations(
    answer: str,
    source_texts: Dict[int, str],
) -> Tuple[str, List[Dict[str, object]]]:
    """回傳 (校正後答案, 修正紀錄)。無標記、無來源或無需修正時原樣返回。

    source_texts: {source_num: 該來源送進 LLM 的文字}。標記編號不在其中者跳過。
    """
    if not answer or not source_texts or "[來源" not in answer:
        return answer, []

    src_tokens = {n: _tokens(t or "") for n, t in source_texts.items()}
    trailing_nl = "\n" if answer.endswith("\n") else ""   # splitlines 會吃掉結尾換行
    lines = answer.splitlines()
    fixes: List[Dict[str, object]] = []
    remap: Dict[int, set] = {}          # 原編號 → 改成過哪些編號（供清單列一致性重寫）

    # ── 第一階段：內容行（含「表格下單獨一行標記」的內容引用）──────────
    for i, line in enumerate(lines):
        if not _MARKER_RE.search(line) or _is_list_item(lines, i):
            continue

        # claim = 本行去掉標記；太短就往前串接（處理「表格 + 下一行單獨標記」）
        claim = _strip_markers(line).strip()
        j = i - 1
        while (len(claim) < _CLAIM_TARGET_CHARS and j >= 0
               and lines[j].strip() and not _MARKER_RE.search(lines[j])):
            claim = lines[j].strip() + "\n" + claim
            j -= 1
            if len(claim) >= _CLAIM_MAX_CHARS:
                break
        claim_toks = _tokens(claim[:_CLAIM_MAX_CHARS])
        if not claim_toks:
            continue

        new_line = line
        for n_str in dict.fromkeys(_MARKER_RE.findall(line)):   # 去重保序
            n = int(n_str)
            if n not in src_tokens:
                continue                       # 指向來源表以外的塊（如 KG 附註）→ 不動
            s_cited = _support(claim_toks, src_tokens[n])
            if s_cited >= _KEEP_MIN:
                continue                       # 原引用站得住 → 保留
            best_n, best_s = n, s_cited
            for m, mt in src_tokens.items():
                s = _support(claim_toks, mt)
                if s > best_s:
                    best_n, best_s = m, s
            if best_n != n and best_s >= _FIX_MIN and best_s >= s_cited + _FIX_MARGIN:
                new_line = new_line.replace(f"[來源{n}]", f"[來源{best_n}]")
                remap.setdefault(n, set()).add(best_n)
                fixes.append({"line": i, "from": n, "to": best_n,
                              "support_from": round(s_cited, 3),
                              "support_to": round(best_s, 3),
                              "claim": claim[:60]})
        if new_line != line:
            # 改標後同一行可能出現重複標記（[來源2][來源2]）→ 收斂成一個
            new_line = re.sub(r"(\[來源(\d+)\])(?:\1)+", r"\1", new_line)
            lines[i] = new_line

    if not fixes:
        return answer, []

    # ── 第二階段：文末「參考來源」清單列的一致性 ─────────────────────
    # 內文的 [來源4] 全改成 [來源2] 之後，清單列的「- [來源4]」也要跟上，
    # 否則畫面上內文與清單對不起來。只在改法唯一時才重寫（保守）。
    body_cited = {int(x) for k, l in enumerate(lines)
                  if not _is_list_item(lines, k)
                  for x in _MARKER_RE.findall(l)}
    seen_list_lines: set = set()
    for i, line in enumerate(lines):
        if not _is_list_item(lines, i):
            continue
        n_strs = _MARKER_RE.findall(line)
        if len(n_strs) == 1:
            n = int(n_strs[0])
            if n not in body_cited and n in remap and len(remap[n]) == 1:
                lines[i] = line.replace(f"[來源{n}]", f"[來源{next(iter(remap[n]))}]")
        key = lines[i].strip()
        if key in seen_list_lines:
            lines[i] = None                    # 重寫後重複的清單列 → 移除
        else:
            seen_list_lines.add(key)

    fixed = "\n".join(l for l in lines if l is not None) + trailing_nl
    logger.info("引用校驗：修正 %d 處 %s", len(fixes),
                ["來源%d→%d(%.2f→%.2f)" % (f["from"], f["to"],
                 f["support_from"], f["support_to"]) for f in fixes])
    return fixed, fixes
