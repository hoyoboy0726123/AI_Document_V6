from __future__ import annotations

import os

# Paddle 3.3.x 的 PIR + OneDNN 新執行器在 Windows CPU 有 regression
# (ConvertPirAttribute2RuntimeAttribute not support [pir::ArrayAttribute<...>])。
# 主要修法是 pyproject.toml 鎖到 paddlepaddle 3.2.2 + paddleocr 3.4.1；
# 下面這幾個 FLAGS 是保險絲，若未來不小心升上去也能多一道防線
# (注意：實測 PIR 新執行器會忽略這些 legacy toggle，所以不能單靠它們)。
# 上游 issue：https://github.com/PaddlePaddle/Paddle/issues/77340
os.environ.setdefault("FLAGS_use_mkldnn", "0")
os.environ.setdefault("FLAGS_use_pir_in_executor", "0")

from dataclasses import dataclass, asdict
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional
import io
import json
import logging
import re

import numpy as np
from PIL import Image

from .pdf_image import get_pdf_page_count, pdf_page_to_image
from ..core.config import settings

logger = logging.getLogger(__name__)

# paddleocr 改為「用到才載入」。
#
# 原本是模組層級 import，於是不管 OCR_ENGINE 設成什麼都會載入 —— 而預設是
# rapid（onnx 三件套），paddle 只是保底用的備援。代價是每次後端啟動、每支碰到
# 文件服務的腳本，都要跑 paddlex 對 huggingface / aistudio / modelscope /
# bcebos 的四次連線檢查，拖慢啟動又多佔記憶體。
#
# 而且它會往 stderr 寫「Checking connectivity to the model hosters…」，
# PowerShell 5.1 把原生指令的 stderr 包成 NativeCommandError，害整條管線中止
# —— 重抽腳本「第一份完成後下一份立刻 exit=1 且零輸出」就是這樣來的。
def _load_paddle(name: str):
    """延遲載入 paddleocr 的類別；未安裝或載入失敗回 None。"""
    try:
        import paddleocr  # type: ignore
        return getattr(paddleocr, name, None)
    except Exception as exc:  # pragma: no cover
        logger.warning("載入 paddleocr.%s 失敗（僅影響 pp_structure 引擎）：%s", name, exc)
        return None


@dataclass
class OCRBlock:
    block_type: str  # paragraph | table | caption | figure_note
    text: str
    page: Optional[int] = None
    paragraph_index: Optional[int] = None
    table_index: Optional[int] = None
    html: Optional[str] = None
    markdown: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def normalize_ocr_blocks(blocks: List[OCRBlock]) -> List[Dict[str, Any]]:
    """將 OCR block 標準化成可供 chunking 使用的 dict 結構。"""
    normalized: List[Dict[str, Any]] = []
    for idx, block in enumerate(blocks):
        text = (block.text or "").strip()
        if not text:
            continue
        normalized.append(
            {
                "block_type": block.block_type,
                "text": text,
                "page": block.page,
                "paragraph_index": block.paragraph_index if block.paragraph_index is not None else idx + 1,
                "table_index": block.table_index,
                "html": block.html,
                "markdown": block.markdown,
                "metadata": block.metadata or {},
            }
        )
    return normalized


class _HTMLTableParser(HTMLParser):
    """Minimal stdlib HTML-table → 2D-grid parser. No new deps."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: List[List[str]] = []
        self._cur_row: Optional[List[str]] = None
        self._cur_cell: Optional[List[str]] = None
        self._in_table = False
        self._span_pending: int = 1  # colspan for current cell

    def handle_starttag(self, tag: str, attrs: List) -> None:
        tag = tag.lower()
        if tag == "table":
            self._in_table = True
        elif tag == "tr" and self._in_table:
            self._cur_row = []
        elif tag in ("td", "th") and self._cur_row is not None:
            self._cur_cell = []
            self._span_pending = 1
            for k, v in attrs:
                if k.lower() == "colspan":
                    try:
                        self._span_pending = max(1, int(v))
                    except Exception:
                        pass
        elif tag == "br" and self._cur_cell is not None:
            self._cur_cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "table":
            self._in_table = False
        elif tag == "tr" and self._cur_row is not None:
            if self._cur_row:
                self.rows.append(self._cur_row)
            self._cur_row = None
        elif tag in ("td", "th") and self._cur_cell is not None and self._cur_row is not None:
            cell_text = re.sub(r"\s+", " ", "".join(self._cur_cell)).strip()
            self._cur_row.append(cell_text)
            # Fill colspan with empty cells
            for _ in range(self._span_pending - 1):
                self._cur_row.append("")
            self._cur_cell = None
            self._span_pending = 1

    def handle_data(self, data: str) -> None:
        if self._cur_cell is not None:
            self._cur_cell.append(data)


def _html_table_to_markdown(html: str) -> str:
    """Convert an HTML <table> string into Markdown pipe-table syntax.

    Uses stdlib html.parser; rejects rows that diverge in column count by padding
    to the widest row. Returns "" if no rows extracted.
    """
    if not html or "<" not in html:
        return ""
    parser = _HTMLTableParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:
        logger.debug("HTML table parse failed: %s", exc)
        return ""

    rows = parser.rows
    if not rows:
        return ""

    width = max(len(r) for r in rows)
    norm = [r + [""] * (width - len(r)) for r in rows]

    md_lines: List[str] = []
    header = norm[0]
    md_lines.append("| " + " | ".join(c or " " for c in header) + " |")
    md_lines.append("| " + " | ".join(["---"] * width) + " |")
    for row in norm[1:]:
        md_lines.append("| " + " | ".join(c or " " for c in row) + " |")
    return "\n".join(md_lines)


def _build_paddle_ocr():
    PaddleOCR = _load_paddle("PaddleOCR")
    if PaddleOCR is None:
        raise RuntimeError("PaddleOCR 未安裝，請先在 backend 環境安裝 paddleocr")

    # PaddleOCR 3.4.0 + paddlepaddle 3.3.1 在 Windows CPU 上：
    # 1) 預設啟用的 doc_orientation / doc_unwarping / textline_orientation 預處理會觸發 segfault
    # 2) 預設的 server 模型 (PP-OCRv5_server_det/rec) 在 rec 階段 segfault；mobile 模型穩定
    # 因此這裡明確關閉預處理並改用 mobile 模型。
    return PaddleOCR(
        lang="ch",
        enable_mkldnn=False,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        text_detection_model_name="PP-OCRv5_mobile_det",
        text_recognition_model_name="PP-OCRv5_mobile_rec",
    )


def _build_pp_structure():
    PPStructureV3 = _load_paddle("PPStructureV3")
    if PPStructureV3 is None:
        raise RuntimeError("PPStructureV3 未安裝，請先 pip install 'paddlex[ocr]'")

    # Windows CPU 已知問題：
    # - MKLDNN/onednn 在 layout/table/det 模型上會在 predictor.run() 丟 "Unknown exception"
    #   (PIR 新執行器會忽略 module 層的 FLAGS_use_mkldnn，必須在建構時明確 enable_mkldnn=False，
    #    與能正常運作的 _build_paddle_ocr() 一致，否則文字偵測推論會 crash → 退回 basic OCR、表格全丟)
    # - server 級 OCR 模型 (PP-OCRv5_server_det/rec) 會 segfault → 改用 mobile
    return PPStructureV3(
        enable_mkldnn=False,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        text_detection_model_name="PP-OCRv5_mobile_det",
        text_recognition_model_name="PP-OCRv5_mobile_rec",
    )


_TEXT_BLOCK_LABELS = {
    "text",
    "paragraph",
    "paragraph_title",
    "doc_title",
    "title",
    "abstract",
    "content",
    "header",
    "footer",
    "reference",
    "list",
}
_TABLE_BLOCK_LABELS = {"table"}
_SKIP_BLOCK_LABELS = {"figure", "image", "chart", "seal", "formula_number"}


def _block_field(block: Any, *keys: str) -> Any:
    """PPStructureV3 block 可能是 dict 或 object — 兩種都支援。"""
    for k in keys:
        if isinstance(block, dict) and k in block:
            return block[k]
        if hasattr(block, k):
            return getattr(block, k)
    return None


def _extract_layout_blocks(page_result: Any) -> List[Any]:
    """從一個 PPStructureV3 page result 取出 layout blocks list。"""
    candidates = (
        "parsing_res_list",
        "layout_parsing_res",
        "parsing_res",
        "layout_det_res",
        "blocks",
    )
    for key in candidates:
        if isinstance(page_result, dict) and key in page_result:
            value = page_result[key]
            if isinstance(value, list) and value:
                return value
        if hasattr(page_result, key):
            value = getattr(page_result, key)
            if isinstance(value, list) and value:
                return value
    return []


_TABLE_RE = re.compile(r"<table\b[^>]*>.*?</table>", re.IGNORECASE | re.DOTALL)


def _replace_html_tables_with_markdown(text: str) -> str:
    """Find every <table>...</table> in `text` and replace it with markdown pipe-table.

    Falls back to the original HTML if our parser can't extract rows (e.g. table
    with rowspan we don't support). Non-table content is left untouched.
    """
    if not text or "<table" not in text.lower():
        return text

    def _sub(match: "re.Match[str]") -> str:
        html = match.group(0)
        md = _html_table_to_markdown(html)
        return f"\n\n{md}\n\n" if md else html

    return _TABLE_RE.sub(_sub, text)


def _ppstructure_page_blocks(pipeline, pdf_path: str, page_num: int) -> List[OCRBlock]:
    """單頁 PP-Structure：轉圖 → predict → markdown → OCRBlock 清單。

    注意：`pipeline.predict()` 在大尺寸/特殊頁面上可能 **native segfault**（直接殺掉整個
    process，Python try/except 攔不到）。in-process 呼叫者請自行承擔；要對單頁崩潰免疫，
    走 `extract_image_pdf_blocks_isolated()`（子進程隔離）。
    """
    blocks: List[OCRBlock] = []
    # paddle CPU 在大尺寸影像上會 native segfault → 壓到安全上限（見 config OCR_MAX_DIMENSION）
    img_bytes = pdf_page_to_image(
        pdf_path, page_num,
        dpi=getattr(settings, "OCR_DPI", 150),
        max_dimension=getattr(settings, "OCR_MAX_DIMENSION", 1600),
    )
    if not img_bytes:
        logger.warning("PPStructure 跳過頁面 %s：轉圖失敗", page_num)
        return blocks

    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    img_array = np.array(img)
    result = pipeline.predict(img_array)

    page_paragraphs = 0
    for page_result in result or []:
        try:
            md_dict = page_result._to_markdown(pretty=False)
        except Exception as exc:
            logger.warning("PPStructure 第 %s 頁 _to_markdown 失敗 (%s)，跳過", page_num, exc)
            continue

        raw_markdown = (md_dict or {}).get("markdown_texts") or ""
        if not raw_markdown.strip():
            continue

        converted = _replace_html_tables_with_markdown(raw_markdown)
        # 以連續空行切段，每段一個 paragraph block。
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", converted) if p.strip()]
        for para in paragraphs:
            page_paragraphs += 1
            has_table = "|" in para and "---" in para
            blocks.append(
                OCRBlock(
                    block_type="table" if has_table else "paragraph",
                    text=para,
                    page=page_num,
                    paragraph_index=page_paragraphs,
                    markdown=para if has_table else None,
                    metadata={"ocr_engine": "ppstructurev3"},
                )
            )
    return blocks


def extract_image_pdf_blocks_with_structure(pdf_path: str) -> List[Dict[str, Any]]:
    """以 PP-StructureV3 做版面分析，輸出含 markdown 表格的逐頁文字（in-process，不隔離）。"""
    pipeline = _build_pp_structure()
    page_count = get_pdf_page_count(pdf_path)
    if page_count <= 0:
        return []

    blocks: List[OCRBlock] = []
    for page_num in range(1, page_count + 1):
        try:
            page_blocks = _ppstructure_page_blocks(pipeline, pdf_path, page_num)
        except Exception as exc:
            logger.warning("PPStructure 第 %s 頁推論失敗 (%s)，整體改用 basic OCR", page_num, exc)
            raise
        blocks.extend(page_blocks)
        logger.info("PPStructure 完成第 %s 頁，抽出 %s 段 markdown", page_num, len(page_blocks))

    return normalize_ocr_blocks(blocks)


def _jsonl_done_pages(path: str) -> set:
    done = set()
    try:
        with io.open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    done.add(json.loads(line).get("page"))
                except Exception:
                    pass
    except Exception:
        pass
    return {p for p in done if p}


def _progress_crashed_page(prog: str) -> Optional[int]:
    """progress 內「最後一個有 START 卻沒 DONE」的頁碼 = native crash 那頁。"""
    started: List[int] = []
    done: set = set()
    for ln in prog.splitlines():
        ln = ln.strip()
        if ln.startswith("START "):
            try:
                started.append(int(ln.split()[1]))
            except Exception:
                pass
        elif ln.startswith("DONE "):
            try:
                done.add(int(ln.split()[1]))
            except Exception:
                pass
    for p in reversed(started):
        if p not in done:
            return p
    return None


def _basic_ocr_single_page(ocr, pdf_path: str, page_num: int) -> List[OCRBlock]:
    """單頁 basic PaddleOCR（純文字），給 PP-Structure 崩潰頁 fallback。"""
    out: List[OCRBlock] = []
    img_bytes = pdf_page_to_image(
        pdf_path, page_num,
        dpi=getattr(settings, "OCR_DPI", 150),
        max_dimension=getattr(settings, "OCR_MAX_DIMENSION", 1600),
    )
    if not img_bytes:
        return out
    try:
        arr = np.array(Image.open(io.BytesIO(img_bytes)).convert("RGB"))
        result = ocr.predict(arr)
    except Exception as exc:
        logger.warning("crashed-page %s basic OCR fallback failed: %s", page_num, exc)
        return out
    li = 0
    for pr in result or []:
        for t in (pr.get("rec_texts", []) or []):
            t = str(t or "").strip()
            if not t:
                continue
            li += 1
            out.append(OCRBlock(
                block_type="paragraph", text=t, page=page_num, paragraph_index=li,
                metadata={"ocr_engine": "paddleocr", "fallback": "crashed_page"},
            ))
    return out


def extract_image_pdf_blocks_isolated(pdf_path: str) -> List[Dict[str, Any]]:
    """子進程隔離版 PP-Structure OCR：每頁在 worker 子進程跑，某頁 native segfault 不會拖垮
    整份；崩潰頁自動退回 basic PaddleOCR（純文字）。worker 模型只載一次，僅在崩潰時重啟。
    """
    import os
    import subprocess
    import sys
    import tempfile

    page_count = get_pdf_page_count(pdf_path)
    if page_count <= 0:
        return []

    backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    tmpdir = tempfile.mkdtemp(prefix="ocr_iso_")
    out_jsonl = os.path.join(tmpdir, "out.jsonl")
    progress = os.path.join(tmpdir, "prog.txt")
    io.open(out_jsonl, "w").close()

    crashed: set = set()
    max_crashes = max(3, page_count // 4 + 1)

    while True:
        done = _jsonl_done_pages(out_jsonl)
        if len(done) + len(crashed) >= page_count:
            break
        run_skip = done | crashed
        io.open(progress, "w").close()
        env = dict(os.environ)
        env.setdefault("FLAGS_use_mkldnn", "0")
        env.setdefault("FLAGS_use_pir_in_executor", "0")
        try:
            subprocess.run(
                [sys.executable, "-m", "app.services.ocr_worker",
                 pdf_path, out_jsonl, progress, ",".join(map(str, sorted(run_skip)))],
                cwd=backend_dir, env=env, capture_output=True,
                timeout=getattr(settings, "OCR_WORKER_TIMEOUT", 3600),
            )
        except subprocess.TimeoutExpired:
            logger.warning("OCR worker 逾時")
            break
        prog = ""
        try:
            with io.open(progress, encoding="utf-8") as f:
                prog = f.read()
        except Exception:
            pass
        if "ALL_DONE" in prog:
            break
        cp = _progress_crashed_page(prog)
        if cp is None or cp in crashed:
            logger.warning("OCR isolated：無法判定崩潰頁（或重複），停止重啟")
            break
        logger.warning("OCR isolated：第 %s 頁讓 PP-Structure native crash，跳過並改用 basic OCR", cp)
        crashed.add(cp)
        if len(crashed) > max_crashes:
            logger.warning("OCR isolated：崩潰頁過多 (%s)，停止", len(crashed))
            break

    page_to_blocks: Dict[int, List[OCRBlock]] = {}
    try:
        with io.open(out_jsonl, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                page_to_blocks[rec.get("page")] = [OCRBlock(**b) for b in rec.get("blocks", [])]
    except Exception as exc:
        logger.warning("OCR isolated：讀取結果失敗 %s", exc)

    if crashed:
        try:
            ocr = _build_paddle_ocr()
            for p in sorted(crashed):
                page_to_blocks[p] = _basic_ocr_single_page(ocr, pdf_path, p)
        except Exception as exc:
            logger.warning("OCR isolated：崩潰頁 basic OCR fallback 失敗 %s", exc)

    all_blocks: List[OCRBlock] = []
    for p in range(1, page_count + 1):
        all_blocks.extend(page_to_blocks.get(p, []))
    logger.info("OCR isolated 完成：%s 頁，崩潰頁 %s（已退回 basic OCR）", page_count, sorted(crashed))
    return normalize_ocr_blocks(all_blocks)


def extract_image_pdf_blocks(
    pdf_path: str,
    *,
    engine: Optional[str] = None,
    tier: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """圖片型 PDF OCR。引擎由 OCR_ENGINE 決定（可用 engine= 覆寫）：

    - rapid（預設）：輕量 onnx 三件套；快、無 paddle/segfault。失敗自動退回 PP-StructureV3。
    - pp_structure：PaddleOCR PP-StructureV3（表格→markdown），再失敗退回 basic PaddleOCR 純文字。

    tier= 可單次覆寫 rapid 後端的 OCR 等級（mobile/server），供高精度重跑使用。
    """
    chosen = (engine or getattr(settings, "OCR_ENGINE", "rapid") or "rapid").lower()
    if chosen == "rapid":
        try:
            from .rapid_ocr import extract_image_pdf_blocks_rapid
            blocks = extract_image_pdf_blocks_rapid(pdf_path, tier=tier)
            if blocks:
                return blocks
            logger.warning("rapid OCR 未抽出任何內容，改用 PP-StructureV3 重試")
        except Exception as exc:
            logger.warning("rapid OCR 失敗，改用 PP-StructureV3：%s", exc)

    # 只有真的要退回 paddle 時才載入它（預設引擎是 rapid，多數情況根本不會走到這）
    if _load_paddle("PPStructureV3") is not None:
        try:
            if getattr(settings, "OCR_SUBPROCESS_ISOLATION", True):
                return extract_image_pdf_blocks_isolated(pdf_path)
            return extract_image_pdf_blocks_with_structure(pdf_path)
        except Exception as exc:
            logger.warning("PPStructureV3 整體流程失敗，改用 basic PaddleOCR：%s", exc)

    ocr = _build_paddle_ocr()
    page_count = get_pdf_page_count(pdf_path)
    if page_count <= 0:
        return []

    blocks: List[OCRBlock] = []

    for page_num in range(1, page_count + 1):
        # paddle CPU 在大尺寸影像上會 native segfault → 壓到安全上限（見 config OCR_MAX_DIMENSION）
        img_bytes = pdf_page_to_image(
            pdf_path, page_num,
            dpi=getattr(settings, "OCR_DPI", 150),
            max_dimension=getattr(settings, "OCR_MAX_DIMENSION", 1600),
        )
        if not img_bytes:
            logger.warning("PaddleOCR 跳過頁面 %s：轉圖失敗", page_num)
            continue

        try:
            img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
            img_array = np.array(img)
        except Exception as exc:
            logger.warning("PaddleOCR 跳過頁面 %s：圖片解碼失敗 (%s)", page_num, exc)
            continue

        result = ocr.predict(img_array)
        page_lines: List[str] = []
        line_idx = 0

        for page_result in result or []:
            rec_texts = page_result.get("rec_texts", []) or []
            rec_scores = page_result.get("rec_scores", []) or []

            for idx, text_value in enumerate(rec_texts):
                text = str(text_value or "").strip()
                score = None
                if idx < len(rec_scores) and rec_scores[idx] is not None:
                    score = float(rec_scores[idx])
                if not text:
                    continue
                page_lines.append(text)
                line_idx += 1
                blocks.append(
                    OCRBlock(
                        block_type="paragraph",
                        text=text,
                        page=page_num,
                        paragraph_index=line_idx,
                        metadata={"ocr_engine": "paddleocr", "confidence": score},
                    )
                )

        logger.info("PaddleOCR 完成第 %s 頁，抽取 %s 行", page_num, len(page_lines))

    return normalize_ocr_blocks(blocks)
