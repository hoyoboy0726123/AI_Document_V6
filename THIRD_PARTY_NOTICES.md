# 第三方元件授權

本專案本身採用 **AGPL-3.0**（見 [LICENSE](LICENSE)）。
以下列出主要相依套件與其授權，供部署與散布前查核。

> 授權資訊取自各套件安裝後的 metadata（`importlib.metadata`），
> 非法律意見。實際散布前建議自行覆核，或洽法務。

## 為什麼本專案是 AGPL

**PyMuPDF 是 AGPL-3.0**（雙授權，另有 Artifex 商業授權）。它與本專案構成
單一作品，因此整體必須以 AGPL 條款提供，除非改用其他 PDF 函式庫或購買商業授權。

PyMuPDF 的用途（用得很淺，未來要抽換不難）：

| 檔案 | 用途 |
|---|---|
| `app/services/kg_headings.py` | 讀字體大小／粗體以偵測章節標題（抽換難度最高，判斷邏輯依賴字體資訊） |
| `app/services/pdf_image.py` | PDF 頁面轉圖（供 VL 視覺分析） |
| `app/services/documents.py` | 取原生文字層，校正 VL 的形近字誤讀 |

若要脫離 AGPL，替代方向是 `pypdfium2`（BSD-3-Clause / Apache-2.0）。
主要工作量在 `kg_headings`：它靠 PyMuPDF 的 span 字體資訊判斷標題，
換掉後需重新驗證整條 KG 抽取流程。

## AGPL 第 13 條（網路互動）的實務要求

只要有人**透過網路使用**本服務（包含公司區網內的同事），就必須讓他們能取得
完整原始碼。本專案的作法：

- repo 公開於 GitHub，且包含完整可建置的原始碼（`requirements.txt`、
  `pyproject.toml`、`package.json`、alembic migrations）
- 前端側邊欄底部固定顯示 `AGPL-3.0 · 原始碼` 連結

## 主要相依與授權

| 套件 | 版本 | 授權 |
|---|---|---|
| **pymupdf** | 1.27.2.3 | **AGPL-3.0 或 Artifex 商業授權** ⚠ |
| psycopg2-binary | 2.9.12 | LGPL（動態連結，不具傳染性） |
| fastapi | 0.136.1 | MIT |
| uvicorn | 0.46.0 | BSD-3-Clause |
| sqlalchemy | 2.0.49 | MIT |
| alembic | 1.18.4 | MIT |
| pydantic / pydantic-settings | 2.13.4 / 2.14.1 | MIT |
| python-jose | 3.5.0 | MIT |
| passlib | 1.7.4 | BSD |
| bcrypt | 4.0.1 | Apache-2.0 |
| python-multipart | 0.0.28 | Apache-2.0 |
| pdfminer.six | 20251230 | MIT |
| pdfplumber | 0.11.9 | MIT |
| httpx | 0.28.1 | BSD |
| faiss-cpu | 1.13.2 | MIT AND BSD-3-Clause |
| numpy | 2.4.4 | BSD-3-Clause 等 |
| Pillow | 12.2.0 | MIT-CMU |
| slowapi | 0.1.9 | MIT |
| opencc-python-reimplemented | 0.1.7 | Apache-2.0 |
| paddleocr / paddlepaddle | 3.4.1 / 3.2.2 | Apache-2.0 |
| sentence-transformers | 5.5.1 | Apache-2.0 |
| transformers | 5.12.0 | Apache-2.0 |
| torch | 2.12.0+cpu | BSD-3-Clause |
| google-genai | 2.8.0 | Apache-2.0 |
| pandas | 3.0.2 | BSD |
| openpyxl | 3.1.5 | MIT |

除 PyMuPDF 外，其餘皆為寬鬆授權（MIT／BSD／Apache-2.0）或不具傳染性的
LGPL，不影響本專案的授權選擇。

## 模型與資料

- **LLM／嵌入／視覺模型**由 Ollama 在本機執行，各有自己的授權
  （`qwen3:8b`、`qwen3-vl:8b-instruct`、`qwen3-embedding:8b`），
  本 repo 不散布模型權重。
- **cross-encoder**（`BAAI/bge-reranker-base`）於首次執行時下載，
  本 repo 不散布權重。
- **語料文件**（MIL-STD 等）不在版本控制內（`storage/` 與資料庫皆已 gitignore），
  各文件的著作權歸原發行單位。

## 重新產生本清單

```bash
cd backend
.venv\Scripts\python.exe -c "import importlib.metadata as md; \
  print('\n'.join(f'{d.metadata[\"Name\"]}\t{d.version}' for d in md.distributions()))"
```
