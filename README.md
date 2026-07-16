# AI_Document_V4 — 軍規標準智慧問答系統（RAG × 知識圖譜）

一套**完全本地化（on-premise）**的文件管理 + OCR + RAG 問答 + 知識圖譜（KG）系統，
針對 **MIL-STD-810H 等測試規範**設計，也相容一般商管／技術文件。
所有推論（LLM、向量嵌入、視覺分析、OCR）皆可跑在自家機器上，**文件與查詢不外流**。

> V4 是在 V3（混合查詢路由 + 序列 KG 佇列）基礎上，經過一次深度安全／穩定性稽核後的**強化版**。
> 若你熟悉 V3，請直接看下方 [「V4 相較 V3 的變更」](#v4-相較-v3-的變更)。

---

## 目錄
- [系統特色](#系統特色)
- [V4 相較 V3 的變更](#v4-相較-v3-的變更)
- [架構總覽](#架構總覽)
- [RAG 檢索流程](#rag-檢索流程)
- [知識圖譜與 Agent](#知識圖譜與-agent)
- [建議規格與模型配置](#建議規格與模型配置) ⭐
- [安裝與啟動](#安裝與啟動)
- [設定參考（.env）](#設定參考env)
- [安全性設計](#安全性設計)
- [專案結構](#專案結構)
- [常見問題](#常見問題)

---

## 系統特色

| 功能 | 說明 |
|---|---|
| 📄 文件管理 | 上傳 PDF、分類／資料夾、多選批次（分類／移動／刪除）、全文檢索 |
| 🔍 OCR | 原生 PDF 用 pdfminer；掃描／圖片型 PDF 用 PaddleOCR（PP-OCRv5，CPU） |
| 🧠 RAG 問答 | 混合檢索（向量 + BM25 關鍵字，RRF 融合）→ cross-encoder 重排 → LLM 有根據地作答並附來源 |
| 🕸️ 知識圖譜 | 自動抽取規範間的引用／取代／定義關係（ISO/IEC/MIL-STD/IEEE/…），可視化查詢 |
| 🤖 ReAct Agent | 多步推理，會自行呼叫檢索／KG 工具，適合「列舉」「關係鏈」類問題 |
| 👁️ 視覺分析（VL） | 對 PDF 頁面做圖表／版面理解（qwen3-vl） |
| 🔀 三模式查詢路由 | 內容題走 RAG、關係／列舉題走 Agent，或自動「混合」判斷 |
| 👥 多使用者隔離 | 每位使用者的分析對話、問答紀錄、筆記彼此獨立 |

---

## V4 相較 V3 的變更

V4 針對一次深度稽核（6 項 Critical + 17 項 High + 多項 Medium）逐條修復，重點如下：

### 🔒 安全性
- **修補註冊提權**：`/auth/register` 的 `role` 改白名單，一般使用者無法自行升級為 admin。
- **修補檔案外洩（LFI）**：`source_pdf_path` 存取加上路徑白名單校驗，杜絕路徑遍歷。
- **權限收斂**：chunk 寫入、資料夾寫入改為 **admin-only**。
- **Refresh Token 原子輪替 + 重用偵測**：token 一次性使用，偵測到重用即撤銷整條授權鏈。
- **速率限制**：登入連續失敗鎖定（指數退避，最長 15 分）、重運算端點配額限制、支援反向代理（`X-Forwarded-For`）。

### 🧩 資料一致性
- **原子化流程**：刪除／重跑 OCR／重新向量化改為「先建後換」，中途失敗不會遺失原資料。
- **Partial ingest 修正**：先確認 embedding 成功才寫入 chunk，避免 DB 與 FAISS 不同步。
- **FAISS 原子寫入**：`.tmp` + `os.replace`，並新增「從 DB 重建索引」端點。
- **刪文件同步清理**：一併移除 KG 邊／孤立節點／使用者分析／筆記。

### 🏗️ 架構與穩定性
- **導入 Alembic 資料庫遷移**：取代 V3 啟動時的 ad-hoc `ALTER TABLE`（baseline 版本 `0001`）。
- **`faiss_id` 改 BigInteger**：相容 PostgreSQL。
- **重啟自癒**：重開機後自動復原卡住（stale）的背景任務。
- **SQLite 加固**：WAL 模式 + `busy_timeout`，降低鎖衝突。
- **同文件重跑去重**：避免重複觸發造成並行衝突。

### 🎨 前端
- 全域 **ErrorBoundary**、換帳號身分即時修正、SSE 斷線 fallback、任務輪詢完成即停 + 多分頁同步。
- 路由 **code-splitting**（加速首屏）。
- **參考來源卡片修復**（可連到 PDF 預覽／存筆記）+ 來源片段以 **Markdown／表格**渲染。
- 文件列表 **多選批次**（分類／移到資料夾／刪除）。
- PDF 預覽的 AI 分析對話**自動捲到最新一次查詢**。

> 完整稽核清單見 [`AUDIT_REPORT_2026-07.md`](AUDIT_REPORT_2026-07.md) 與 [`AUDIT_FIXES_V4.md`](AUDIT_FIXES_V4.md)。

---

## 架構總覽

兩個獨立行程：

```
┌─────────────────────┐        /api 反向代理        ┌──────────────────────────┐
│  frontend (Vite)    │  ───────────────────────▶  │  backend (FastAPI)       │
│  React 18 + antd     │                            │  app.main:app            │
│  zustand + Router 6  │  ◀── SSE 串流（RAG/Agent） │  SQLAlchemy + SQLite/PG   │
└─────────────────────┘                            └────────────┬─────────────┘
                                                                │
                          ┌─────────────────────────────────────┼───────────────────────┐
                          ▼                     ▼                ▼                        ▼
                    ┌───────────┐        ┌────────────┐   ┌─────────────┐        ┌──────────────┐
                    │  Ollama    │        │ PaddleOCR  │   │   FAISS      │        │ cross-encoder │
                    │ LLM/VL/嵌入│        │ (CPU)      │   │ IndexIDMap2  │        │ reranker(CPU) │
                    └───────────┘        └────────────┘   └─────────────┘        └──────────────┘
```

- **backend/** — FastAPI。路由在 `app/api/v1/`（`auth`/`documents`/`folders`/`rag`/`vector_search`/`tasks`/`admin`/`kg`/`agent`…），業務邏輯在 `app/services/`，ORM 在 `app/models.py`。
- **frontend/** — React 18 + Vite + Ant Design（zh-TW）+ zustand + React Router 6，所有 API 走 `/api` 由 Vite 代理到後端。
- **LLM Provider 抽象** — `services/llm_provider/` 提供 `OllamaProvider` / `GeminiProvider`，可從管理介面切換（預設 Ollama，全本地）。

---

## RAG 檢索流程

```
PDF ─┬─ 原生文字 → pdfminer.six
     └─ 掃描/圖片 → PaddleOCR(PP-OCRv5, CPU)
        │
        ▼
     切塊(chunk) ──▶ qwen3-embedding:8b 嵌入 ──▶ FAISS IndexIDMap2 (storage/faiss_index.bin)
        │                                              │
        ▼                                              ▼
   查詢 ──▶ 混合檢索：向量相似 + BM25 關鍵字（RRF 融合，撈寬）
        ──▶ cross-encoder 重排（BAAI/bge-reranker-base，CPU）取 Top-K
        ──▶ gemma4:12b 有根據地綜合作答（附來源片段，可點回 PDF）
```

- **混合檢索**（`RAG_HYBRID_SEARCH=True`）：對「靠關鍵字才找得到的分散子項目」recall 大幅提升，適合規範條號。
- **重排**（`RAG_RERANK=True`）：先撈寬（`RAG_RERANK_POOL=12`）再由 cross-encoder 精排，低分剔除。
- 每個 `DocumentChunk` 存自己的 `faiss_id`，確保 DB 與 FAISS 索引同步。

---

## 知識圖譜與 Agent

- **知識圖譜**：`kg_extractor` 以正則抽取規範代號並正規化 → `kg_relations.classify_pair` 讓 LLM 標註關係。
  關係詞彙為**封閉集**：`references | supersedes | defines | requires | derives_from`；
  低於 `KG_MIN_CONFIDENCE`（預設 0.3）的邊會剔除。
  抽取以背景任務串列執行（`KG_AUTO_EXTRACT=True` 時 ingest 後自動觸發）。
- **ReAct Agent**（`services/agent.py`）：每回合輸出一個嚴格 JSON 動作，呼叫工具（`rag_search`／`spec_lookup`／`spec_references`／`spec_supersedes_chain`／`document_get`），
  以 SSE 串流 `thought / tool_call / observation / final` 事件。適合「A 引用了哪些規範」「取代鏈」類問題。

---

## 建議規格與模型配置

> 這是完全本地推論的系統，**GPU VRAM 是主要瓶頸**。以下依 VRAM 分三級，並附最小 8GB 方案。

### 目前預設使用的模型（皆 Ollama，Q4_K_M 量化）

| 角色 | 模型 | 佔用 | 說明 |
|---|---|---|---|
| LLM（作答／KG 關係） | `gemma4:12b` | **7.6 GB VRAM** | 主力生成模型 |
| 視覺分析（VL） | `qwen3-vl:8b-instruct` | **6.1 GB VRAM** | PDF 圖表／版面理解 |
| 向量嵌入 | `qwen3-embedding:8b` | **4.7 GB VRAM** | ingest 與查詢時嵌入 |
| OCR | PaddleOCR PP-OCRv5（server tier） | CPU（~數百 MB RAM） | 掃描 PDF 文字辨識，不佔 VRAM |
| 重排 | `BAAI/bge-reranker-base` | CPU | cross-encoder 精排，不佔 VRAM |

**關鍵觀念**：LLM、VL、嵌入三者**多在不同階段使用**（ingest 用嵌入、查詢用 LLM、PDF 分析用 VL），
很少真正同時常駐。Ollama 會依 `OLLAMA_KEEP_ALIVE` 自動換入／換出模型，所以 VRAM 不足時可靠「序列載入」硬跑。

### 🟢 最小方案：8 GB VRAM（例：RTX 5060 8G）

**可以跑，但 LLM 需序列載入、速度會受限。** 因為 `gemma4:12b`（7.6GB）幾乎吃滿 8GB，
加上 KV cache／context 會溢位 → Ollama 自動把少數層放到 CPU（能跑但變慢）。建議調整：

```env
# backend/.env
OLLAMA_NUM_CTX=4096        # 降 context 省 VRAM（預設 8192）
OLLAMA_KEEP_ALIVE=0        # 不同階段用完即釋放，避免多模型同時常駐
```

- **策略**：嵌入（4.7GB）、VL（6.1GB）、LLM（7.6GB）「一次只載一個」→ 8GB 夠用。
- **想更順**：把 LLM 換成更小的（如 `qwen3:8b` ≈ 5GB）就能全程常駐、免換模，代價是回答品質略降。
- **⚠️ 驅動需求**：RTX 5060 屬 **Blackwell（sm_120）**，需 **NVIDIA driver 570+ / CUDA 12.8+**，否則 Ollama 無法使用 GPU。
- OCR 與 reranker 走 CPU，不影響。
- **建議搭配**：系統記憶體 **≥ 16GB**（OCR + reranker + FAISS + Python）。

### 🟡 建議方案：12–16 GB VRAM（例：RTX 4070 / 4080 / 5070）

- LLM（7.6GB）+ 嵌入（4.7GB）**可同時常駐**（約 12.3GB）→ 查詢與 ingest 交錯不必反覆換模，順暢許多。
- VL 分析時再載入（此時通常沒在跑 LLM）。
- `OLLAMA_NUM_CTX=8192`、`OLLAMA_KEEP_ALIVE=5m`（預設值即可）。
- 系統記憶體 **≥ 32GB** 更佳。

### 🔵 理想方案：24 GB+ VRAM（例：RTX 4090 / 5090）

- 可升級 LLM 至 `gemma4:26b`（17GB）提升回答品質，或全模型常駐 + 更大 context。
- 適合多人同時使用、大批量 ingest。

### 其他硬體

| 項目 | 最低 | 建議 |
|---|---|---|
| CPU | 4 核 | 8 核以上（OCR／reranker／BM25 吃 CPU） |
| RAM | 16 GB | 32 GB |
| 磁碟 | 模型約 20GB + 文件儲存空間 | SSD |

---

## 安裝與啟動

### 先決條件
- **Python 3.11 或 3.12**（`requires-python = ">=3.11,<3.13"`）
- **Node.js 18+**（前端）
- **Ollama**（先在主機上啟動，**不要跑在 Docker 內**）

### 1) 拉取模型（Ollama）

```bash
ollama pull gemma4:12b
ollama pull qwen3-vl:8b-instruct
ollama pull qwen3-embedding:8b
```

> 8GB VRAM 若要更省，可改拉 `qwen3:8b` 當 LLM，並在 `.env` 把 `OLLAMA_LLM_MODEL` 指向它。

### 2) 後端

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate            # Windows CMD
pip install -r requirements.txt   # 或 uv sync（若用 uv）

copy .env_example .env            # 然後編輯 .env（見下方）
alembic upgrade head              # 套用資料庫遷移
```

> **必填**：`.env` 的 `SECRET_KEY` 需 ≥ 32 字元，否則後端**不會啟動**。

### 3) 前端

```bash
cd frontend
npm install
```

### 4) 啟動（Windows 一鍵）

```
launch_AI_Document_V3.bat
```

會開兩個 CMD 視窗：
- 後端：`127.0.0.1:8001`（`uvicorn app.main:app --port 8001`）
- 前端：`localhost:5175`（`npm run dev -- --port 5175`）

手動啟動：

```bash
# 後端
cd backend
.venv\Scripts\python.exe -u -m uvicorn app.main:app --host 127.0.0.1 --port 8001

# 前端（另一個視窗）
cd frontend
set VITE_API_TARGET=http://127.0.0.1:8001
npm run dev -- --host --port 5175
```

- 健康檢查：`GET http://127.0.0.1:8001/health`
- 預設管理員：帳號／密碼由 `.env` 的 `DEFAULT_ADMIN_USERNAME` / `DEFAULT_ADMIN_PASSWORD` 決定（**首次登入後請立即改密碼**）。

---

## 設定參考（.env）

| 變數 | 預設 | 說明 |
|---|---|---|
| `SECRET_KEY` | （必填，≥32 字） | JWT 簽章金鑰，**勿提交、勿硬編** |
| `DATABASE_URL` | SQLite | 可換 PostgreSQL（`postgresql+psycopg://…`） |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Ollama 位置 |
| `OLLAMA_LLM_MODEL` | `gemma4:12b` | 作答模型 |
| `OLLAMA_VISION_MODEL` | `qwen3-vl:8b-instruct` | 視覺分析模型 |
| `OLLAMA_EMBED_MODEL` | `qwen3-embedding:8b` | 嵌入模型 |
| `OLLAMA_NUM_CTX` | `8192` | context 長度（8GB VRAM 建議降 4096） |
| `OLLAMA_KEEP_ALIVE` | `5m` | 模型常駐時間（8GB 建議 `0`） |
| `EMBEDDING_MAX_CHARS` | `2000` | 單段嵌入字數上限 |
| `DEFAULT_TOP_K` | `5` | 回傳來源數 |
| `MAX_PDF_ANALYSIS_PAGES` | `10` | VL 分析頁數上限（受模型 context 限制） |
| `OCR_MODEL_TIER` | `mobile` | `mobile`（快）/ `server`（準） |
| `RAG_HYBRID_SEARCH` | `True` | 向量 + BM25 混合檢索 |
| `RAG_RERANK` | `True` | cross-encoder 重排 |
| `KG_AUTO_EXTRACT` | `True` | ingest 後自動抽取知識圖譜 |
| `KG_MIN_CONFIDENCE` | `0.3` | KG 邊信心門檻 |

> 也可用 `LLM_PROVIDER=gemini` / `EMBEDDING_PROVIDER=gemini` 改走雲端（需 `GEMINI_API_KEY`）；視覺分析僅支援 Ollama。

---

## 安全性設計
- **JWT + 可撤銷 Refresh Token**：refresh token 存 DB、一次性使用、偵測重用即撤銷授權鏈。
- **註冊角色白名單**、**chunk／資料夾 admin-only**、**source PDF 路徑白名單校驗**。
- **速率限制**：登入指數退避鎖定、重運算端點配額、proxy-aware IP。
- **敏感資訊**：一律走 `.env`，`SECRET_KEY` 等**不進版控**（見 `.gitignore`）。
- **多使用者資料隔離**：分析對話／問答紀錄／筆記皆以 `user_id` 範圍化。

---

## 專案結構

```
AI_Document_V4/
├── backend/
│   ├── app/
│   │   ├── api/v1/          # 路由：auth, documents, rag, kg, agent…
│   │   ├── services/        # RAG / OCR / 向量 / KG / Agent / LLM provider
│   │   ├── models.py        # SQLAlchemy ORM
│   │   ├── core/config.py   # 設定（Pydantic Settings）
│   │   └── main.py          # 進入點、啟動自癒、Alembic 遷移
│   ├── alembic/             # 資料庫遷移（baseline 0001）
│   └── .env_example         # 設定範本（複製為 .env 後填寫）
├── frontend/
│   └── src/                 # pages / components / stores / services
├── AUDIT_REPORT_2026-07.md  # 稽核發現與未來規劃
├── AUDIT_FIXES_V4.md        # V4 逐項修復對照
└── launch_AI_Document_V3.bat
```

---

## 常見問題

**Q：一定要 GPU 嗎？**
A：強烈建議。純 CPU 可跑 OCR 與 reranker，但 LLM／嵌入／VL 在 CPU 上會非常慢。至少 8GB VRAM 才有實用速度。

**Q：8GB VRAM 真的能跑 12B 模型？**
A：可以，但 `gemma4:12b`（7.6GB）會小幅溢位到 CPU、速度受限。照上面「最小方案」把 `NUM_CTX` 降到 4096、`KEEP_ALIVE=0`，或改用 `qwen3:8b` 更順。

**Q：文件會外流嗎？**
A：不會。預設全走本地 Ollama，文件、向量、查詢都留在你的機器。除非你主動把 provider 切成 Gemini（雲端）。

**Q：如何備份？**
A：備份 `backend/doc_management.db`（或你的 PostgreSQL）、`backend/storage/`（含 FAISS 索引與原始 PDF）即可。

---

_本專案自 V3 演進而來，V4 聚焦安全性、資料一致性與可維運性。問題回報請開 Issue。_
