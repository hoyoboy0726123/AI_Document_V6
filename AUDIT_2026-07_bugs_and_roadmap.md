# AI_Document_V3 — 深度審查報告與未來開發計劃

> 2026-07-07 · 由主導模型帶 5 個同模型子代理分頭審查(RAG 管線 / KG+Agent / API 安全 / 前端 / 商管通用性),再由主導模型逐條核對、去重、交叉驗證彙整。
> 標記說明:`[C]`Critical `[H]`High `[M]`Medium `[L]`Low。★ = 兩個以上獨立子代理同時發現(可信度最高)。

---

## 0. 結論摘要(先看這段)

- **系統的「純 RAG 檢索層」做得紮實**:hybrid(向量+BM25 RRF)+ cross-encoder 重排、中文切塊、per-user 隔離、SSE 授權、bcrypt、無 raw SQL —— 這些都是對的。
- **真正致命的是兩類**:(1)**安全**有一條完整攻擊鏈(任意人可提權成 admin → 再讀走 .env/DB);(2)**資料一致性**沒有任何原子路徑,crash 或例外會留下「DB 與 FAISS 單邊狀態」,且**沒有重建工具**當逃生門。
- **KG/Agent 增強層**設計克制,但**生命週期沒閉環**(重啟/刪除/重跑都會壞),且路由/guard 的 regex 彼此優先序矛盾,最容易「答非所問」。
- **一個隱形的查準殺手**:embedding 前硬截 450 字,但預設 chunk 1800 字 → 每個 chunk 只有前 25% 進向量;後段內容只剩 BM25 撈得到。這是「平常測不出、上線才發現查不到」的類型。★
- **商管資料**:純 RAG 層幾乎零改動可用;KG/Agent 層會整層退化(不崩,但變慢+文案錯位);唯一「會答錯」的是財報表格數字問答。

---

## 1. 必須立刻修(Critical)

### C1 ★ 任意人可註冊成 admin(提權)
`api/v1/auth.py:208` — `role=user.role or "assistant"`,`/register` 無認證、直接採信 request body 的 role。
打 `POST /auth/register {"role":"admin"}` 即取得管理員,繞過所有 `get_current_admin_user`。3/hour rate limit 擋不住(只需一次)。
**修:** register 硬編 `role="assistant"`,`create_user` 對 role 做白名單。

### C2 ★ 建文件 `source_pdf_path` 未鎖目錄 → 任意檔案外洩(LFI)
`services/documents.py:272-308`(`_resolve_temp_pdf` 只檢查 exists())。
給 `source_pdf_path=./.env` 或 `./doc_management.db` → `shutil.move` 搬進可下載目錄 → `GET /{id}/pdf` 下載,取得 SECRET_KEY / DB / 密碼雜湊;且 move 具破壞性。與 C1 串成完整攻擊鏈。
**修:** `validate_file_path(candidate, PDF_TEMP_DIR)`,只收暫存目錄內 .pdf;或只接受伺服器組回的 uuid.pdf。

### C3 ★ 刪文件時 `logger` 未定義 → NameError + FAISS/DB 不一致
`services/documents.py:246`(全模組無 `logger` 定義,已確認 import 區無 logging)。
刪除時 PDF 刪檔 except 分支呼叫 `logger.warning` → NameError;此時 FAISS 向量已刪(:231)、DB 尚未 commit → 回滾後**DB 有 chunk、FAISS 無向量**,API 500。最常見觸發:pdf_path 落在 FILE_STORAGE_DIR 之外時 validate_file_path 先拋 403。
**修:** 加 `logger = logging.getLogger(__name__)`;刪除順序改「先 DB commit → 再刪 FAISS → 最後刪檔」。

### C4 重跑 OCR「先刪後建」→ 中途失敗永久資料遺失
`services/documents.py:329-338` — 舊 chunks+向量在新內容產生前就刪除並 commit;之後 OCR/VL/embedding 任一步失敗,文件變 0 chunks 且原文不可得,無還原。
**修:** 先產生新 chunks+embeddings 成功,再於同交易替換,最後才動 FAISS。

### C5 ★ App 重啟後排隊中的 KG / 背景任務永遠 pending
`kg_queue.py:24`(純記憶體 queue)+ `main.py:255`(startup 無 stale-task 掃描)。
批次上傳排隊中重啟 → 其餘任務永遠 pending,前端 banner 永遠轉圈。ocr_status 也可能永卡 processing。
**修:** startup 掃 `status in ('pending','running')`:pending 重排、running 標 failed。

### C6 步驟 0 列舉早退無 relation/app guard → 答非所問
`agent.py:588-604`(迴圈「前」的早退只檢查 `_looks_like_enumeration`,缺後面 834/794 行才有的 guard)。
「MIL-STD-810H **引用了哪些**標準?」被 588 攔下 → `list_subitems` 命中 810H 文件節點 → 回「共 N 個方法」收工,引用問題沒被回答;應用題「該做哪些測試」同樣被劫持成方法清單。
**修:** 588 條件補 `and not _RELATION_RE.search(q) and not _APP_RE.search(q)`,並要求 matched 實體名等於列舉對象才早退。

---

## 2. 高優先(High)

### 資料一致性 / 檢索品質
- **H1 ★ embedding 硬截 450 字 + 超長 chunk 不再切** — `ollama_client.py:695-701`(截 450)/`pdf_processing.py:198-237`(單一超長 segment 不切)。預設 chunk 1800 字只有前 25% 進向量,後段只剩 BM25。**這是最大的隱性查準缺陷。** 修:截斷上限隨 embedding 模型設定化;超長 segment 沿句界強切。
- **H2 FAISS index 寫入非原子** — `vector_store.py:46,62` 直接覆寫;寫入中斷電 → 整檔損毀、所有檢索死亡,且**無重建工具**。修:寫 `.tmp` 後 `os.replace()`;加「從 DB embedding 全量重建」admin 端點。
- **H3 換 embedding 模型維度不一致無防護** — `vector_store.py:19-32` 載入既有 index 忽略 dimension;切模型後檢索 500、re-vectorize 打出索引。修:add 時檢查 `index.d != vectors.shape[1]`;切模型強制全量重嵌入。
- **H4 partial ingest** — `documents.py:425-455` 先 commit 空 embedding chunks 再 embed;embed 失敗 → DB 有文字、FAISS 無向量,還被 BM25 建索引回傳幽靈 faiss_id。修:embed 成功後才 insert chunks。
- **H5 ★ 刪文件/重向量化不清 KG 邊、user analysis、tasks** — `documents.py:220-250`。SQLite 未開 FK → 懸空邊+幽靈節點;**PostgreSQL 直接 FK violation,刪除必 500**。修:delete 內先 `delete_kg_for_document` + 刪 `doc:{id}%` 結構實體 + 清 user analysis。
- **H6 faiss_id 在 PostgreSQL 溢位** — `models.py:222` Integer(32-bit)存 63-bit 值 → Postgres 首次上傳必炸。修:改 BigInteger + ALTER。
- **H7 KG worker 可永久卡 running / 堵死整條佇列** — `kg_pipeline.py:243` classify_pair 無 timeout;Ollama hang 一次 = 唯一 worker 永久阻塞。修:provider chat 加 timeout + per-task wall-clock 上限。
- **H8 同 spec-pair 每個 chunk 重複送 LLM** — `kg_pipeline.py:236-251`。(810H,IEC60068)出現 50 chunk = 打 50 次 LLM。千頁文件上萬次無效呼叫,是佇列堵塞主因。修:document 級 (src,dst) seen-set。

### KG 抽取正確性
- **H9 IEEE 帶字母後綴塌陷** — `kg_extractor.py:80-87`。`IEEE 802.11b`/`802.1Q`/`802.3` 全併成 `IEEE 802`,邊掛錯節點。修:pattern 保留後綴。

### API 安全
- (見 §1 C1/C2;另 Medium 一批見 §3)

### 前端
- **H10 ★ 換帳號登入拿到前一人身分** — `api.js:104-106` 攔截器無條件用舊 token 覆蓋登入請求 → B 的 token 綁 A 的 user(還影響 admin 判斷)。修:`if(!config.headers.Authorization && token)`;登入前先 logout。
- **H11 斷線後 UI 永久卡「進行中」** — `QAConsolePage.jsx:145-166` 收不到 done 無 fallback。修:reader 結束補呼叫 onDone / finally 清 loading。★(與後端 SSE 無心跳 H 對應)
- **H12 切頁不 abort 串流(洩漏+答案消失)** — `QAConsolePage.jsx` 無 unmount cleanup;PdfPreviewModal 更嚴重:分析中換文件,舊文件分析文字寫進新文件對話。修:useEffect cleanup abort。
- **H13 全站無 ErrorBoundary** — 任一頁 render 錯 = 整站白屏。修:Routes 外包 ErrorBoundary。
- **H14 StrictMode 下答案存兩次** — setState updater 內藏 setState 副作用。修:commit 邏輯移出 updater。
- **H15 連續送出兩條 stream 互寫同一答案** `[C→實測H]` — handleSubmit 開頭無 `if(loading)return`。修:加 guard。

### 路由誤判
- **H16 ★ 英文子串誤判**`agent.py:236-241` — `requires` 命中 *required*、`version` 命中 *conversion*。內容題被路由到 agent。修:英文詞加 `\b`。
- **H17 `_APP_RE` 過寬** `agent.py:256` — 「設備/產品/安裝」出現就封殺所有 KG 確定性答案。修:改要求主語意圖詞。

---

## 3. 中優先(Medium)— 摘要

**後端資料/併發**
- 同文件並行重建無 per-document 鎖(documents.py:329/457)
- FTS 同步靠「列數相等」heuristic,會靜默過期(hybrid_search.py:74-84)
- 每編輯一 chunk 就整檔重寫 400MB index,持鎖阻塞所有檢索(vector_store.py:43-62)
- 掃描件 OCR 在 HTTP 請求內同步跑數分鐘(documents.py:345-359)
- SQLite 未開 WAL/busy_timeout/foreign_keys(database.py:11-14)
- list() 全表載入 Python 分頁;/vector-search/health 載全 embedding + N+1

**API 安全**
- chunk CRUD 只需登入即可竄改任意文件檢索內容(documents.py:1373-1575)
- 預設密碼 Admin@123 每次重啟被重設回、且無改密端點(main.py:126-167)
- refresh token 輪替非原子 + 無 reuse detection(auth.py:84-136)
- 多 worker 下 ensure_schema_updates DDL 競爭(main.py) → 應導入 Alembic
- rate limit 以 IP 為 key,proxy 後全員共用(auth.py:23-27)
- KG/VL/OCR 重運算端點無配額(kg.py/rag.py)
- 資料夾任何登入者可刪、連帶影響他人文件(folders.py:69-147)

**KG/Agent**
- delete→extract 非原子:重跑失敗清空原 KG(kg_pipeline.py:311)
- 重跑保留 entities → 孤兒 section 節點污染頁界計算(kg_service.py:231)
- IEC legacy 編號(68-2-52)漏抓 / ISO 帶年份碎裂成多節點(kg_extractor.py)
- `_SPEC_ID_RE` fallback 刪空白後對不上 ISO/IEC/ASTM canonical(agent.py:898)
- section_lookup / `_build_spec_relation_block` 寫死 `MIL-STD-810H` 字串(agent.py:865/340)
- scratchpad 無總量預算,多步後塞爆本地模型 context(agent.py:777)
- SSE 中途斷線對話不落庫 + 無心跳(api/v1/agent.py)
- 跨文件同編號 section 無消歧(agent_tools.py:627)

**前端**
- 任務完成後輪詢不停(TaskStatusContext.jsx:57);多分頁 removeTask 用 stale state 蓋 localStorage
- SSE fetch 繞過 axios:token 過期只噴 401 不 refresh(QAConsolePage.jsx:125)
- PDF 頁碼越界開壞頁 / token refresh 觸發整份 PDF 重載(PdfPreviewModal.jsx)
- 清除歷史與進行中 stream 競態;KG loadGraph 無競態防護

---

## 4. 交叉主題(根因)

1. **沒有「原子」與「逃生門」** — FAISS↔DB 全程無原子操作,index 檔非原子寫,又沒有從 DB 重建 index 的工具。→ 一次 crash 就可能永久壞。
2. **生命週期沒閉環** — 重啟(任務 pending)、刪除(孤兒/單邊)、重跑(先刪後建)三個動作都缺善後。
3. **regex 當語意用** — 路由、guard、實體反向比對大量靠無 `\b` 的子串比對,彼此優先序又矛盾 → 誤路由/答非所問。
4. **單機假設寫死在架構裡** — vector_store 與 kg_queue 都是進程內單例;**絕不可開 uvicorn --workers**(建議 main.py 啟動時偵測 workers>1 直接拒啟)。
5. **領域寫死** — MIL-STD-810H / ASUS 字串、規範用語散落在 prompt、guard、前端文案。

---

## 5. 商管資料通用性結論

**可以,分兩層:**
- **純 RAG 問答層零改動可用** — 檢索、中文切塊、BM25(CJK 友好)、qwen3-embedding、reranker、RAG prompt 全領域無關。
- **KG/Agent 增強層整層退化**(不崩,處處 fallback):KG 近空圖(SOP 裡的 ISO 9001 仍抓得到)、中文標題建不出節點、路由 regex 過敏(「參考/版本/取代」)把商管題繞一圈沒用的規範工具再繞回 RAG → 答案對但慢。

**風險排序:**
1. 財報表格數字問答(大表被切+跨列運算靠 LLM 心算)—— 唯一「會答錯」,改動 L
2. 中文結構抽取全空(kg_headings 不認中文標題)—— M
3. route_mode 誤路由 —— 只傷延遲,S
4. 列舉 block 偶發答非所問 —— M
5. 文案/prompt 規範用語殘留 —— 純觀感,S

**最小上線路徑(1~2 天):** 改 route regex(去商管高頻詞或改「KG 有料才走 agent」動態 gate)+ agent system prompt 領域中性 + 前端文案 → 即可當通用文件問答上線。

---

## 6. 未來開發計劃(分階段)

### 第 0 階段 — 止血(1 週,先做這些再談其他)
| 項目 | 對應 | 量 |
|---|---|---|
| 註冊禁止指定 role + create_user 白名單 | C1 | S |
| source_pdf_path 鎖 PDF_TEMP_DIR | C2 | S |
| documents.py 補 logger + 刪除順序改「先 commit 後刪 FAISS」 | C3 | S |
| startup 掃 stale 任務(pending 重排 / running failed) | C5 | S |
| 步驟 0 列舉早退補 relation/app guard | C6 | S |
| embedding 截斷上限設定化 + 超長 segment 強切 | H1 | M |
| FAISS 原子寫(.tmp+os.replace)+ 從 DB 重建 index 端點 | H2 | M |

### 第 1 階段 — 資料一致性與可靠性(2~3 週)
- 重跑 OCR/向量化改「先建後換」原子流程(C4/H3/H4)
- 刪文件/重向量化清 KG 邊+孤兒節點+user analysis(H5)
- SQLite PRAGMA(WAL+busy_timeout+foreign_keys)一次到位
- per-document 鎖 / 拒絕同文件並行任務(M)
- KG worker 加 timeout + 每 task wall-clock 上限(H7)
- KG pair 跨 chunk 去重快取(H8,LLM 呼叫直接砍 1~2 個數量級)
- 啟動偵測 workers>1 直接拒啟 + 文件註明單機限制

### 第 2 階段 — 安全加固(1~2 週)
- 改密端點(改密後撤銷 refresh)+ 移除預設弱密碼 + ensure_default_admin 只建不重設
- refresh token 原子輪替 + reuse detection
- chunk CRUD / 資料夾寫入改 admin-only
- 導入 Alembic 取代啟動時 ad-hoc DDL
- rate limit 處理 X-Forwarded-For + 帳號層級節流;重運算端點加配額

### 第 3 階段 — 前端韌性(1~2 週)
- 統一 SSE client(frame 解析 + done fallback + abort + token refresh)
- 全域 ErrorBoundary;登入前 logout + 攔截器不覆蓋自帶 Authorization(H10)
- unmount/換文件 abort 串流(H12);StrictMode 副作用修正(H14);送出 loading guard(H15)
- 任務輪詢:完成即停 + 多分頁 storage 事件同步 + 可見性暫停
- 對話 memo 化 + 串流節流;LLM 輸出過 DOMPurify;路由 code-splitting

### 第 4 階段 — KG/Agent 精修(2~3 週)
- 路由 regex 加 `\b` + 弱訊號 tie-breaker(先 extract_specs,無 spec id 改 rag)(H16/H17)
- 去除 MIL-STD-810H/ASUS 寫死字串,改動態判斷(M)
- IEEE/IEC/ISO 抽取修正(後綴保留、legacy、年份 alias)(H9+M)
- supersedes 寫入端做方向/家族 sanity check
- KG 垃圾回收(清 degree-0 誤抓節點)
- scratchpad 總量預算 + SSE 心跳與斷線落庫

### 第 5 階段 — 通用化 / 商管支援(視需求,3~6 週)
- route_mode + agent prompt + 前端文案領域中性化(先做,1~2 天可上線純 RAG 商管)
- kg_headings 支援中文標題(第X條/一、二、)餵進現成 Document/Section schema
- 通用實體抽取(公司/人名/日期/金額:regex + LLM NER)
- 關係詞彙擴充(party_of/amends/effective_during…)
- **表格理解升級(財報數字問答關鍵)**:table block 專屬 chunking(整塊不切+表頭隨片段複製)+ 計算型問題走工具(計算器/表格 SQL)
- 合約條款定位工具 clause_lookup(複製 section_lookup 模式)

---

## 附:已確認做對的地方(不要動壞)
SSE 用 header 帶 token(不進 log)、SSE generator 自開自關 SessionLocal、admin 端點後端驗 DB role、per-user 隔離查詢無橫向越權、PDF 下載有 validate_file_path、bcrypt + secrets token、無 raw SQL、hybrid RRF + cross-encoder 重排、中文切塊與 BM25 CJK、KG 確定性層設計克制、KG 序列佇列確實解了 SQLite 撞鎖。
