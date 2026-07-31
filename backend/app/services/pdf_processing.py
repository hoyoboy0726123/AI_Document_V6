import logging
import re
import statistics
from collections import Counter
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

BLOCK_TYPE_PARAGRAPH = "paragraph"
BLOCK_TYPE_TABLE = "table"

import pdfplumber
from pdfminer.pdfdocument import PDFPasswordIncorrect

from .. import schemas

logger = logging.getLogger(__name__)

_EMPTY_CELL_THRESHOLD = 0.7  # 空白儲存格超過此比例視為空白表格


def _cell_text(cell) -> str:
    return str(cell).replace("\n", " ").strip() if cell else ""


# 段落軟上限：超過就沿句子邊界切成多個 segment（不截斷、不遺失內容）。
# 取代舊的「>1000 字直接截斷加 ...」做法，避免規範條文/表格說明後半段消失。
_PARAGRAPH_SOFT_LIMIT = 1500
# 句子邊界：中文標點 。！？； 與英文 .!?; 以及換行
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？；!?;\.\n])\s*")


def _split_long_paragraph(text: str, soft_limit: int = _PARAGRAPH_SOFT_LIMIT) -> List[str]:
    """將過長段落沿句子邊界切成 <= soft_limit 的片段；短段落原樣回傳。"""
    if len(text) <= soft_limit:
        return [text]
    pieces: List[str] = []
    buf = ""
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        if not sentence:
            continue
        if buf and len(buf) + len(sentence) > soft_limit:
            pieces.append(buf)
            buf = sentence
        else:
            buf += sentence
    if buf:
        pieces.append(buf)
    return pieces or [text]


def _is_mostly_empty(table_data: List[List], threshold: float = _EMPTY_CELL_THRESHOLD) -> bool:
    cells = [cell for row in table_data for cell in row]
    if not cells:
        return True
    empty = sum(1 for c in cells if not _cell_text(c))
    return (empty / len(cells)) >= threshold


def _format_table_markdown(table_data: List[List]) -> str:
    """將 pdfplumber table 格式化為 markdown pipe 表格。"""
    rows = []
    for row in table_data:
        cells = [_cell_text(c) for c in row]
        rows.append("| " + " | ".join(cells) + " |")
    return "\n".join(rows)


def _table_header_summary(table_data: List[List]) -> str:
    """空白表格只取第一行非空標題。"""
    if not table_data:
        return ""
    header_cells = [_cell_text(c) for c in table_data[0] if _cell_text(c)]
    return " | ".join(header_cells)


def _extract_rotated_text(page) -> str:
    """把頁面上「旋轉 90 度」的文字依正確閱讀順序取出。

    pdfplumber 的 extract_words 只處理直立字元；旋轉字元會以字元的原始順序
    出現，結果整段是顛倒的。實測 MIL-STD-810H 第 216 頁的圖表註記變成

        .sruoh 42 eht tuohguorht Cº72 ta tnatsnoc ylraeN

    也就是「Nearly constant at 27ºC throughout the 24 hours.」全部反過來，
    連數字都反轉（001→100、59→95）。這些是晝夜循環圖的資料註記，內容有用，
    但以這種形式進向量庫只會變成雜訊。

    排序依據以實測決定：字元矩陣為 (0, b, c, 0) 代表旋轉 90 度，
      b > 0（逆時針）→ 逐欄由左至右、欄內由下往上（x0 遞增、top 遞減）
      b < 0（順時針）→ 反向
    在第 216 頁上，前者可還原出正確英文，其餘三種排序都不行。
    """
    rot = [c for c in page.chars if not c.get("upright", True)]
    if not rot:
        return ""

    def _ccw(c) -> bool:
        m = c.get("matrix")
        return bool(m) and len(m) >= 2 and m[1] > 0

    # 同一頁可能同時有兩種旋轉方向 —— 曲線圖的資料標註與座標軸標籤常相反。
    # 先前用「多數決決定整頁方向」，結果資料標註還原了、軸標籤仍是顛倒的：
    #     Constant at 95 percent  )tnecrep( ytidimuH evitaleR
    #     （前半正確，後半是 Relative Humidity (percent) 反轉）
    # 改為依方向分組，各自用自己的排序鍵。
    groups = [
        ([c for c in rot if _ccw(c)], lambda c: (round(c["x0"], 0), -c["top"])),
        ([c for c in rot if not _ccw(c)], lambda c: (-round(c["x0"], 0), c["top"])),
    ]

    lines: List[str] = []
    for chars, key in groups:
        if not chars:
            continue
        # 依 x0 分欄；同一欄的字元屬於同一段旋轉文字
        cur_x: Optional[float] = None
        buf: List[str] = []
        for c in sorted(chars, key=key):
            x = round(c["x0"], 0)
            if cur_x is None or abs(x - cur_x) <= _ROTATED_COLUMN_TOLERANCE:
                buf.append(c["text"])
            else:
                if "".join(buf).strip():
                    lines.append("".join(buf).strip())
                buf = [c["text"]]
            cur_x = x
        if "".join(buf).strip():
            lines.append("".join(buf).strip())
    return "\n".join(lines)


# 旋轉文字分欄的 x0 容差（同一段旋轉文字的字元 x0 會有些微差異）
_ROTATED_COLUMN_TOLERANCE = 6.0


def _extract_page_content(page) -> str:
    """
    從 pdfplumber page 提取內容：
    - 有意義的表格 → markdown pipe 格式
    - 空白表格 → 只保留標題行
    - 其餘文字 → 保留空白佈局（layout=True）
    """
    # 旋轉字元要先從頁面濾掉，再做表格與文字抽取。
    #
    # 只把還原後的旋轉文字「附加」在後面是不夠的：那些字元同時落在表格區域內，
    # find_tables() 會照樣以錯誤順序抽出，結果同一頁裡正確版本與亂碼版本並存
    #   ✗ .sruoh 95 ta 42 97 tnatsnoc eht 98 tuohguorht    ← 表格路徑產生的
    #   ✓ Nearly constant at 27ºC (80ºF) throughout…       ← 還原後附加的
    # 實測濾掉旋轉字元後，該頁反轉詞由 8 個降為 0，且表格數不變（2 個仍是 2 個）。
    rotated_text = _extract_rotated_text(page)
    page = page.filter(lambda o: o.get("upright", True) is not False)

    # 先找出所有表格及其位置
    table_objects = page.find_tables()
    table_bboxes = []
    table_parts: List[Tuple[float, str]] = []  # (y_top, formatted_text)

    for tobj in table_objects:
        data = tobj.extract()
        if not data:
            continue
        y_top = tobj.bbox[1]
        table_bboxes.append(tobj.bbox)

        if _is_mostly_empty(data):
            summary = _table_header_summary(data)
            if summary:
                table_parts.append((y_top, f"[空白記錄表: {summary}]"))
        else:
            table_parts.append((y_top, _format_table_markdown(data)))

    # 取得非表格區域的文字（過濾掉落在表格 bbox 內的文字）
    words = page.extract_words(keep_blank_chars=False, use_text_flow=True)
    non_table_words: List[Dict[str, Any]] = []

    for word in words:
        wx_center = (word["x0"] + word["x1"]) / 2
        wy_center = (word["top"] + word["bottom"]) / 2
        in_table = any(
            bx0 <= wx_center <= bx2 and by0 <= wy_center <= by2
            for bx0, by0, bx2, by2 in table_bboxes
        )
        if not in_table:
            non_table_words.append(word)

    # 以「容差」把同一視覺行的字群聚起來。
    #
    # 舊版用 round(word["top"], 1) 當 key，但 ± 與上標數字（m3 的 3、ft3 的 3）
    # 在 PDF 裡的 top 會比同行一般字高約 1 point，四捨五入後仍落在不同 key，
    # 同一行因而被拆成多組；再依 y 排序合併，順序就錯亂。實測 MIL-STD-810H
    # 第 271 頁被拆成四組，輸出變成
    #     "g/m3 g/ft3). / ±7 / (0.3 ±0.2 / …at 10.6"
    # 單位與公差跑到句首、數值留在句尾，答題時等於拿到殘缺資料。
    # 抽樣 40 頁：62% 的頁面分行結果會改變，其中 18% 含「數值+單位」被打散。
    #
    # 容差取字高中位數的一半（行距通常遠大於字高，不會誤併相鄰兩行）。
    text_parts: List[Tuple[float, str]] = []
    if non_table_words:
        heights = [w["bottom"] - w["top"] for w in non_table_words if w["bottom"] > w["top"]]
        tolerance = max(1.5, statistics.median(heights) * 0.5) if heights else 2.0

        grouped: List[Dict[str, Any]] = []
        for word in sorted(non_table_words, key=lambda w: (w["top"], w["x0"])):
            if grouped and abs(word["top"] - grouped[-1]["top"]) <= tolerance:
                grouped[-1]["words"].append(word)
            else:
                grouped.append({"top": word["top"], "words": [word]})

        for group in grouped:
            # 行內依左右位置排序，避免上標字被排到句首
            ordered = sorted(group["words"], key=lambda w: w["x0"])
            line = " ".join(w["text"] for w in ordered)
            if line.strip():
                text_parts.append((group["top"], line))

    # 按 y 位置合併所有部分
    all_parts = text_parts + table_parts
    all_parts.sort(key=lambda x: x[0])

    body = "\n".join(text for _, text in all_parts).strip()
    if rotated_text:
        body = (body + "\n" + rotated_text).strip() if body else rotated_text
    return body


def extract_text_and_segments(pdf_bytes: bytes) -> Tuple[str, List[schemas.DocumentSegment]]:
    try:
        segments: List[schemas.DocumentSegment] = []
        sections: List[str] = []

        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            for page_number, page in enumerate(pdf.pages, start=1):
                page_text = _extract_page_content(page)
                if not page_text:
                    continue

                sections.append(page_text)
                paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", page_text) if p.strip()]

                for idx, paragraph in enumerate(paragraphs, start=1):
                    normalized = re.sub(r"[ \t]+", " ", paragraph)
                    # 不再截斷；過長段落沿句子邊界切成多個 segment，內容完整保留。
                    for piece in _split_long_paragraph(normalized):
                        segments.append(
                            schemas.DocumentSegment(page=page_number, paragraph_index=idx, text=piece)
                        )

        combined_text = "\n\n".join(sections).strip()
        if combined_text:
            return combined_text, segments

    except PDFPasswordIncorrect:
        raise
    except Exception as exc:
        logger.warning("pdfplumber extraction failed, falling back to pdfminer: %s", exc)

    # Fallback：退回 pdfminer 純文字提取
    try:
        from pdfminer.high_level import extract_text
        plain_text = (extract_text(BytesIO(pdf_bytes)) or "").strip()
        return plain_text, []
    except PDFPasswordIncorrect:
        raise
    except Exception as exc:
        logger.debug("Plain text extraction also failed: %s", exc)
        return "", []


def suggest_keywords(text: str, limit: int = 8) -> List[str]:
    if not text:
        return []
    words = re.findall(r"[A-Za-z0-9\u4e00-\u9fff]{2,}", text.lower())
    stopwords = {"the", "and", "with", "this", "that", "from", "have", "will", "shall"}
    filtered = [w for w in words if w not in stopwords]
    counter = Counter(filtered)
    return [word for word, _ in counter.most_common(limit)]


def split_segments_into_chunks(
    segments: List[schemas.DocumentSegment],
    *,
    max_chars: int = 1800,
    overlap_chars: int = 250,
) -> List[Dict[str, Any]]:
    """
    將段落合併成向量塊，支援 overlap 重疊設計
    """
    chunks: List[Dict[str, Any]] = []
    current_text: List[str] = []
    current_page: Optional[int] = None
    current_paragraph: Optional[int] = None
    chunk_index = 0

    # Audit H1（第二半）：先把「單一超過 max_chars 的 segment」沿句界切開，
    # 句界也切不動的（如整塊無標點的表格 markdown）再硬切，確保沒有任何 chunk
    # 遠超 max_chars（否則後段內容會被 embedding 截斷、永遠查不到）。
    exploded: List[Tuple[str, Optional[int], Optional[int]]] = []
    for segment in segments:
        if isinstance(segment, dict):
            seg_text = (segment.get("text") or "").strip()
            seg_page = segment.get("page")
            seg_para = segment.get("paragraph_index")
        else:
            seg_text = (segment.text or "").strip()
            seg_page = segment.page
            seg_para = segment.paragraph_index
        if not seg_text:
            continue
        if len(seg_text) <= max_chars:
            exploded.append((seg_text, seg_page, seg_para))
            continue
        for piece in _split_long_paragraph(seg_text, max_chars):
            if len(piece) <= max_chars:
                exploded.append((piece, seg_page, seg_para))
            else:
                for i in range(0, len(piece), max_chars):
                    exploded.append((piece[i:i + max_chars], seg_page, seg_para))

    for segment_text, seg_page, seg_para in exploded:
        if not segment_text:
            continue

        prospective = "\n".join(current_text + [segment_text]) if current_text else segment_text
        if current_text and len(prospective) > max_chars:
            full_text = "\n".join(current_text)
            chunks.append(
                {
                    "chunk_index": chunk_index,
                    "page": current_page,
                    "paragraph_index": current_paragraph,
                    "block_type": BLOCK_TYPE_PARAGRAPH,
                    "text": full_text,
                }
            )
            chunk_index += 1

            if len(full_text) > overlap_chars:
                overlap_text = full_text[-overlap_chars:]
                current_text = [overlap_text, segment_text]
            else:
                current_text = [segment_text]

            current_page = seg_page
            current_paragraph = seg_para
        else:
            if not current_text:
                current_page = seg_page
                current_paragraph = seg_para
            current_text.append(segment_text)

    if current_text:
        chunks.append(
            {
                "chunk_index": chunk_index,
                "page": current_page,
                "paragraph_index": current_paragraph,
                "block_type": BLOCK_TYPE_PARAGRAPH,
                "text": "\n".join(current_text),
            }
        )

    return chunks
