# AI_Document_V4 — 稽核修復報告

> V4 為 V3 的完整複本（含 .venv / node_modules，74,290 檔案一致）。
> 本文件記錄依 `AUDIT_2026-07_bugs_and_roadmap.md` 在 V4 實作的修復與驗證結果。
> 日期：2026-07-07。

## 驗證總覽（全部通過）

| 驗證方式 | 結果 |
|---|---|
| 後端 `import app.main`（含 startup） | ✅ OK；C5 復原邏輯在真實 DB 上實際觸發（重排 1 個 KG 任務、標記 3 個死任務失敗） |
| 針對性單元測試 `scripts/verify_audit_fixes.py` | ✅ **23 passed / 0 failed** |
| C1 提權 端對端（TestClient 註冊 role=admin） | ✅ 回傳與 DB role 皆為 assistant，提權被擋 |
| C3+H5 刪除路徑 執行期測試（建→刪→驗清理） | ✅ 無 NameError；chunks/關係/doc 前綴節點清除，共享 spec 節點保留 |
| 真實 FAISS index 完整性（原子寫後） | ✅ ntotal=7546 dim=4096，無殘留 .tmp |
| 前端 `npm run build` | ✅ built（4497 modules） |
| 前端 `npm run lint`（--max-warnings 0） | ✅ 0 error / 0 warning |

---

## Critical（6/6 全修）

- **C1 註冊提權** — `api/v1/auth.py`：register 強制 `role="assistant"`；`services/users.py`：`create_user` 加 `_ALLOWED_ROLES` 白名單。**端對端驗證：role=admin 被強制降為 assistant。**
- **C2 source_pdf_path LFI** — `services/documents.py::_resolve_temp_pdf`：只取檔名、以暫存目錄重組、`validate_file_path` 二次確認、限 .pdf。**驗證：`../../.env`、系統檔、非 .pdf 全被拒。**
- **C3 刪除 NameError + 不一致** — `documents.py`：補 `logger`；刪除順序改「先 DB commit → 再刪 FAISS → 最後刪檔」。**執行期驗證通過。**
- **C4 重跑 OCR「先刪後建」資料遺失** — `_rebuild_document_chunks`：改「先建後換」，extract+embed 成功後才在同一交易刪舊加新；失敗時舊資料完好。
- **C5 重啟後任務永遠 pending** — `main.py::recover_stale_tasks()`：kg_extract 重排、其他標 failed、卡住的 ocr_status 重設。**在真實 DB 上實際觸發。**
- **C6 步驟0 列舉早退答非所問** — `agent.py`：早退加 `not _RELATION_RE` + `not _APP_RE` guard，與迴圈後方一致。

## High（全修）

- **H1 embedding 硬截 450** — 改 `settings.EMBEDDING_MAX_CHARS`（預設 2000）；`pdf_processing` 超長 segment 沿句界/硬切。**驗證：設定生效。**
- **H2 FAISS 非原子寫 + 無重建工具** — `_write_index_atomic`（.tmp+os.replace）；新增 `rebuild_index()` + admin 端點 `POST /vector-search/rebuild-index`。**驗證：重建 2 向量可檢索、無殘留 .tmp。**
- **H3 換模型維度不一致** — `add_embeddings` 檢查 `index.d != dim` 給明確 ValueError。**驗證通過。**
- **H4 partial ingest** — 改「先 embed 成功才寫 chunks」，不再留「有文字無向量」半成品。
- **H5 刪文件不清 KG** — `delete()` 清 KG 邊 + doc 前綴結構節點 + user analysis + notes + 背景任務置 NULL；`delete_kg_for_document` 增購 doc 前綴實體清理。**執行期驗證通過。**
- **H6 faiss_id PostgreSQL 溢位** — `models.py` 改 `BigInteger`。**驗證：欄位型別為 BigInteger。**
- **H7 KG worker 卡死/堵塞** — 已由 `OLLAMA_TIMEOUT`（httpx timeout）自然界定上限；另 `kg_queue._run` 加固：import 失敗不整條死、任務例外標 failed 不卡 pending。
- **H8 PDF 分析忽略 error 事件** — `PdfPreviewModal`：處理 `event:error`、失敗不再顯示「分析完成」、移除 debug log。
- **H9 IEEE 後綴塌陷** — `kg_extractor` 捕捉群加 `[A-Za-z]{0,2}`。**驗證：802.11b / 802.1Q / 1156 不再塌陷成 IEEE 802。**
- **H10 換帳號拿到前一人身分** — `api.js` 攔截器：已自帶 Authorization 就不覆蓋；`LoginPage` 登入前先 logout。
- **H11 斷線 UI 永久卡進行中** — 兩個 SSE reader 結束時若未收到 done/error → 補呼叫 onDone。
- **H12 切頁/換文件不中止串流** — QAConsole unmount cleanup abort；PdfPreviewModal open/documentId 變更時 abort。
- **H13 全站無 ErrorBoundary** — 新增 `components/ErrorBoundary.jsx`，包住 `<Routes>`，任一頁出錯顯示可回復畫面而非白屏。
- **H14 StrictMode 答案存兩次** — onDone 改用 `streamingMsgRef` 讀最終值，setConversationHistory 移出 updater（3 處）。
- **H15 連續送出兩條 stream 互寫** — handleSubmit / handleFollowupSubmit 開頭 `if (loading) return`。
- **H16 英文子串誤路由** — route regex 英文詞加 `\b`（required/conversion/derivative 等），補 successor/下一版。**驗證：8 條 route 案例全對。**
- **H17 `_APP_RE` 過寬** — 移除裸名詞「產品/設備/安裝」，只認使用者意圖措辭。**驗證：關係題含「設備」不再誤觸；真應用題仍觸發。**

## Medium（已修）

- SQLite PRAGMA：WAL + busy_timeout=10s + synchronous=NORMAL（`database.py`）。**驗證：journal_mode=WAL、busy_timeout=10000。**
- chunk CRUD（建/改/刪/合併/拆分）改 admin-only。
- 資料夾 建/改/刪 改 admin-only。
- M5：純 RAG 新問題只送 `{question, answer}` 歷史（不再送整包）。
- clearHistory 先中止進行中串流 + 清 expandedSnippets。
- 登出清 `activeTasks` localStorage。
- PDF 預覽頁碼夾在 [1, numPages]。

---

## 第二輪（2026-07-08）：延後項全數完成（僅剩改密端點依使用者指示不做）

**後端**
- ✅ **refresh token 原子輪替 + 重用偵測** — `core/security.py::rotate_refresh_token`：單一原子 UPDATE 搶佔（rowcount==1 勝出）；已撤銷 token 被重用 → `revoke_all_user_tokens` 全家撤銷。**端對端驗證：首次輪替成功→重用 401→全家失效→重新登入正常。**
- ✅ **Alembic 導入** — `alembic.ini` + `alembic/env.py`（URL 取自 app 設定、SQLite batch mode、不呼叫 fileConfig 以免關閉 app logging）+ `versions/0001_baseline.py`（冪等：create_all(checkfirst) + 守衛式欄位補丁，新/舊 DB 皆安全）。`main.py::run_db_migrations()` 程式化 upgrade head；alembic 檔缺失（frozen build）時退回 legacy 路徑。**驗證：真實 DB 已 stamp `0001_baseline`，重啟 log 顯示 "Alembic migrations applied (head)"。**
- ✅ **rate limit XFF + 帳號層節流 + 重運算配額** — `utils/ratelimit.py`：`client_ip_key`（僅當直接對端為 loopback 才採信 XFF 首 IP，防偽造）換掉兩處 `get_remote_address`；login 加 username 指數退避鎖（5 次失敗鎖 30s 起、倍增、上限 15 分）；`check_quota` 滑動視窗配額掛上 kg/extract(20/hr)、analyze-pdf-pages ×2(共用 30/hr)、upload/async(60/hr)。KG extract 順帶加同文件 pending/running 去重。**驗證：退避 429、配額第 21 次 429。**

**前端**
- ✅ **任務輪詢修正** — `TaskStatusContext`：completed/failed 即停輪詢（doneRef）；`storage` 事件多分頁同步；persist 以 localStorage 現值合併（不再 stale state 覆寫）；poll 的 JSON.parse 包 try/catch；分頁隱藏時暫停輪詢。
- ✅ zustand named import（v4 棄用警告）。
- ✅ antd v5 棄用屬性全清 — `bodyStyle→styles.body`、`destroyOnClose→destroyOnHidden`（antd 5.27 支援）、`Spin tip` 加子元素；共 8 檔。
- ✅ PDF 高亮修正 — regex `lastIndex` 重設；棄 `innerHTML` 改純 DOM `createElement("mark")`（含 `<`/`&` 文字不再被解析、消除注入面）；防 mark 重複包裹與零長匹配。
- ✅ 路由 code-splitting — 全頁面 React.lazy + Suspense。**首屏 bundle 2,488 kB → 743 kB**，react-pdf / force-graph 各自拆包。
- ✅ PrivateRoute / AppHeader 改細粒度 selector。

**第二輪驗證**：verify_audit_fixes.py 23/23、新功能端對端 4/4 PASS、`npm run lint` 0 錯誤、`npm run build` 成功、後端重啟 smoke（Alembic/login/documents 40/KG 2985 實體）全過。

## 唯一未做（依使用者指示）
- **改密端點 + 移除預設 admin 密碼** — 使用者明示不做。部署提醒：請於 `.env` 覆寫 `DEFAULT_ADMIN_PASSWORD`。
- `PRAGMA foreign_keys=ON` 維持不開（歷史資料安全考量；應用層清理已涵蓋孤兒問題，未來可在新 alembic revision 中配 ondelete 一起導入）。

---

## 新增檔案
- `backend/scripts/verify_audit_fixes.py` — 針對性驗證腳本（23 檢查）
- `frontend/src/components/ErrorBoundary.jsx`
- 本報告 `AUDIT_FIXES_V4.md`

## 執行驗證指令
```
cd backend && .venv\Scripts\python.exe scripts/verify_audit_fixes.py
cd frontend && npm run build && npm run lint
```
