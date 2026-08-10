# AI_Document_V6 — 軍規標準智慧問答系統（RAG × 知識圖譜）

一套**完全本地化（on-premise）**的文件管理 + OCR + RAG 問答 + 知識圖譜（KG）系統，
針對 **MIL-STD-810H 等測試規範**設計，也相容一般商管／技術文件。
所有推論（LLM、向量嵌入、視覺分析、OCR）皆可跑在自家機器上，**文件與查詢不外流**。

> **V6 開發中。** 主軸是把問答頁改成 ChatGPT 式的聊天介面：
> 單一輸入框（系統自行判斷新問題／追問）、多對話串、設定收折。
> 詳見 [V6 開發目標](#v6-開發目標)。
>
> V6 由 V5 完整複製而來並保留全部 commit 歷史（起點 4693fc3）。
> V5 的成果與品質數字見下方各章節，仍然適用。

---

## 目錄
- [系統特色](#系統特色)
- [V6 開發目標](#v6-開發目標)
- [目前品質水準](#目前品質水準)
- [V5 相較 V4 的變更](#v5-相較-v4-的變更)
- [回歸評測](#回歸評測)
- [架構總覽](#架構總覽)
- [RAG 檢索流程](#rag-檢索流程)
- [知識圖譜與 Agent](#知識圖譜與-agent)
- [建議規格與模型配置](#建議規格與模型配置)
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
| 🔍 OCR | 原生 PDF 用 pdfminer；掃描／圖片型 PDF 用 PaddleOCR（PP-OCRv5，CPU，延遲載入） |
| 🧠 RAG 問答 | 混合檢索（向量 + BM25，RRF 融合）→ cross-encoder 重排 → LLM 有根據地作答並附來源 |
| 🕸️ 知識圖譜 | 自動抽取規範間的引用／取代／定義關係（ISO/IEC/MIL-STD/IEEE/…），可視化查詢 |
| 🤖 ReAct Agent | 多步推理，自行呼叫檢索／KG 工具，適合「列舉」「關係鏈」類問題 |
| 👁️ 視覺分析（VL） | 對 PDF 頁面做圖表／版面理解，並用原生文字層校正形近字誤讀 |
| 🔀 三模式查詢路由 | 內容題走 RAG、關係／列舉題走 Agent，或自動「混合」判斷 |
| 🛡️ 確定性防線 | 主體一致性查核、假前提標註、無依據數值標註、空表格降級 |
| 👥 多使用者隔離 | 每位使用者的分析對話、問答紀錄、筆記彼此獨立 |

---

## V6 開發目標

| 項目 | 說明 | 狀態 |
|---|---|---|
| 聊天介面 | 單欄對話、設定收折進抽屜，取代目前左右分欄 + 兩個輸入框 | 規劃中 |
| **多對話串** | `user_conversations` 目前主鍵就是 `user_id`，每人只有一筆扁平清單。需新增 `conversations` 表、messages 外鍵、列表與切換 API，並遷移既有歷史 | 規劃中 |
| 新問題／追問自動判斷 | 合併輸入框後由確定性規則推斷（`_find_subject` / `_history_subject` / `resolve_followup_question`），**判斷結果須在送出前顯示成可覆蓋的標籤** | 規劃中 |

> 判斷一定會有漏網（實測「冷凝測試方法與條件」曾被誤判為追問），
> 所以可覆蓋的標籤是這個設計的安全網，不是選配。

---

## 目前品質水準

以 `backend/scripts/eval/` 的兩套評測量得，**連續三次執行結果完全相同**（生成端已固定 temperature 與 seed）。

### 數值題（30 題）＋ 語料內不存在題（5 題）

| 指標 | 數值 | 說明 |
|---|---|---|
| recall@20 | **0.900** | 正解有沒有進候選池 |
| hit@5 | **0.733** | rerank 後有沒有排進前 5 |
| MRR | **0.554** | 正解的平均倒數排名 |
| 數值落地率 | **0.667** | 正解數值有沒有真的寫進答案 |
| **幻覺率** | **0.000** | 語料內不存在的題目卻給出數值 |

### 八種題型（24 題）

| 題型 | 分數 | 題型 | 分數 |
|---|---|---|---|
| 列舉（有哪些 ANNEX） | **1.000** | 跨方法陷阱 | **1.000** |
| 規範取代關係 | **1.000** | 假前提陷阱 | **1.000** |
| 跨文件比較 | **1.000** | 追問主體繼承 | **1.000** |
| 規範引用關係 | 0.889 | 目的／適用範圍 | 0.750 |

> **各型評分標準不同，不要平均在一起看。** 每型只有 2–4 題，統計上偏薄；
> 分數用於偵測回歸，不宜當作絕對能力值。

---

## V5 相較 V4 的變更

### 建立可重複的回歸評測（這一版的核心）

沒有基線就無法判斷一組改動是淨賺還是淨賠。V5 先建立評測，再談優化：

- `run_eval.py` — 數值題 30 + 不存在題 5，三層指標（候選池 → 排序 → 生成），逐題記錄
- `run_eval_types.py` — 列舉／引用／取代／目的／跨方法陷阱／假前提／跨文件／追問共 8 型
- ground truth 全部從語料或 KG 反查驗證，不寫死頁碼

### 語料品質

| 項目 | 改善 |
|---|---|
| 抽取亂碼（`(cid:N)`） | 15.2% → **0.02%** |
| 浮水印區塊 | 1279 → **72** |
| 反轉／旋轉文字 | 9 份文件 → **0** |
| VL 轉寫的形近字（如料號 `22SW0`→`22SWO`） | 以 PDF 原生文字層校正 |
| VL prompt 範例被模型照抄進語料 | 已濾除並改用可辨識的佔位符 |

### 檢索與生成

- **混合檢索**：BM25 中文查詢的英文詞回退、規範編號改為文件過濾而非嵌入內容
- **重排**：`snippet` 加大到 1800 字元；移除有害的「融合第一名保底置頂」
- **跨方法污染防線**：問句主體與證據章節不符時，先用英文詞重查，救不回來就誠實說沒有
- **假前提標註**：問句自帶的數值若在語料中查無依據，於答案前明確標示
- **追問主體繼承**：「那濕度呢」會自動補成「鹽霧測試的濕度」，不再跳到別的測試
- **檢索範圍**：Agent 與混合模式現在都遵守介面上鎖定的文件／資料夾（V4 只有純 RAG 遵守）
- **章節標籤**：`document_chunks.section_path` 由 KG 標題回填，用真實方法邊界取代「±15 頁」近似

### 穩定性

- 補查預算改為每請求共用（修正單次查詢卡住數分鐘）
- 日誌改輪替（`app.log` 曾長到 16.2 GB）
- PaddleOCR 改延遲載入（app 啟動 1.6 秒，預設不載入）
- 前端輸入框與訊息列表解耦，打字不再重繪整個對話記錄

### 誠實記錄：五個「業界最佳實踐」在本語料實測無效

| 建議 | 實測結果 |
|---|---|
| 查詢端 instruct prefix（非對稱嵌入） | 淨賠 |
| rerank pool 12 → 24／40 | 更差且更慢 |
| `min_similarity` 0.5 → 0.55 | 無差異 |
| 更大的 reranker 模型 | 診斷正確但藥方無效 |
| 章節階層回灌 chunk | 六項指標全部零變化 |

共同原因：這些建議是對「典型 RAG 語料」給的。本語料 41 份文件裡只有
MIL-STD-810H 有章節結構，且文件全英文而使用者以中文提問。
**任何檢索或生成層的改動，請先用評測量過再決定保留。**

---

## 回歸評測

```bash
cd backend

# 只跑檢索層（不呼叫 LLM，約 2.5 分鐘）
.venv\Scripts\python.exe scripts\eval\run_eval.py

# 含生成層（約 10 分鐘）
.venv\Scripts\python.exe scripts\eval\run_eval.py --with-generation --baseline scripts\eval\baseline_v2.json

# 八種題型（約 10 分鐘）
.venv\Scripts\python.exe scripts\eval\run_eval_types.py --baseline scripts\eval\types_v6.json
```

單元測試（純函式，秒級）：

```bash
.venv\Scripts\python.exe scripts\test_subject_gate.py          # 主體一致性查核
.venv\Scripts\python.exe scripts\test_followup_subject.py      # 追問補主體
.venv\Scripts\python.exe scripts\test_empty_table_gate.py      # 空表格降級
.venv\Scripts\python.exe scripts\test_section_scope.py         # 章節感知的證據收斂
.venv\Scripts\python.exe scripts\test_vl_repair.py             # VL 形近字校正
.venv\Scripts\python.exe scripts\test_retry_budget_streaming.py # 補查預算跨串流
```

> ⚠️ **評測必須獨佔資源。** 兩個評測併行、或評測與伺服器同時打 LLM，
> 會量到不存在的回歸（實測曾出現 hit@5 0.733→0.700 的假警報）。

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
        ──▶ qwen3:8b 有根據地綜合作答（附來源片段，可點回 PDF）
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
| LLM（作答／KG 關係） | `qwen3:8b` | **~5.2 GB VRAM** | 主力生成模型 |
| 視覺分析（VL） | `qwen3-vl:8b-instruct` | **6.1 GB VRAM** | PDF 圖表／版面理解 |
| 向量嵌入 | `qwen3-embedding:8b` | **4.7 GB VRAM** | ingest 與查詢時嵌入 |
| OCR | PaddleOCR PP-OCRv5（server tier） | CPU（~數百 MB RAM） | 掃描 PDF 文字辨識，不佔 VRAM |
| 重排 | `BAAI/bge-reranker-base` | CPU | cross-encoder 精排，不佔 VRAM |

**關鍵觀念**：LLM、VL、嵌入三者**多在不同階段使用**（ingest 用嵌入、查詢用 LLM、PDF 分析用 VL），
很少真正同時常駐。Ollama 會依 `OLLAMA_KEEP_ALIVE` 自動換入／換出模型，所以 VRAM 不足時可靠「序列載入」硬跑。

### 🟢 最小方案：8 GB VRAM（例：RTX 5060 8G）

**可以跑，但 LLM 需序列載入、速度會受限。** 因為 `qwen3:8b`（7.6GB）幾乎吃滿 8GB，
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
- `OLLAMA_NUM_CTX=24576`、`OLLAMA_KEEP_ALIVE=5m`（預設值即可）。
- 系統記憶體 **≥ 32GB** 更佳。

### 🔵 理想方案：24 GB+ VRAM（例：RTX 4090 / 5090）

- 可升級 LLM 至更大模型（如 `gemma3:27b`）提升回答品質，或全模型常駐 + 更大 context。
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
ollama pull qwen3:8b
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
| `OLLAMA_LLM_MODEL` | `qwen3:8b` | 作答模型 |
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
AI_Document_V5/
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
A：可以，但 `qwen3:8b`（7.6GB）會小幅溢位到 CPU、速度受限。照上面「最小方案」把 `NUM_CTX` 降到 4096、`KEEP_ALIVE=0`，或改用 `qwen3:8b` 更順。

**Q：文件會外流嗎？**
A：不會。預設全走本地 Ollama，文件、向量、查詢都留在你的機器。除非你主動把 provider 切成 Gemini（雲端）。

**Q：如何備份？**
A：備份 `backend/doc_management.db`（或你的 PostgreSQL）、`backend/storage/`（含 FAISS 索引與原始 PDF）即可。

---

_本專案自 V3 演進而來，V4 聚焦安全性、資料一致性與可維運性。問題回報請開 Issue。_
