# 部署與啟動說明（2026-08-10）

本專案有**兩種啟動模式**，用途不同，請不要混用。

---

## 模式一：正式模式（給區網使用者）

```
build_frontend.bat          # 建置前端 → 部署到 backend\frontend_dist
launch_AI_Document_V3.bat   # 啟動服務（0.0.0.0:8001）
```

- **單一埠 8001**，後端同時供應 API 與建置後的前端（`backend/standalone_launcher.py` 掛載 `frontend_dist`）。
- 前後端同源，不需要 vite 代理，也沒有 CORS 問題。
- **vite / esbuild 完全不在服務路徑上**，所以它們那幾個未修補的 dev server 漏洞在這個模式下不成立。
- 區網使用者用 `http://<本機IP>:8001` 存取（用 `ipconfig` 查 IP）。

> ⚠️ **改完前端程式碼必須重跑 `build_frontend.bat` 再重啟服務**，否則區網使用者看到的還是舊的 bundle。這是正式模式的代價，換來的是安全性與效能。

## 模式二：開發模式（只給本機）

```
launch_dev.bat
```

- 後端 `127.0.0.1:8001` + vite dev server `localhost:5175`，有熱更新。
- **刻意不加 `--host`**，dev server 只綁本機。
- 原因：vite 5 的 dev server 有未修補的路徑穿越漏洞（`server.fs.deny` bypass、esbuild 允許任意網站讀取 dev server 回應），修補版在 vite 7/8，而本專案的 `package.json` 鎖在 `^5.0.0`。一旦綁 `0.0.0.0`，同區網的人就能讀取這台機器上的檔案。

---

## ⚠️ .bat 檔必須維持純 ASCII

**不要在任何 `.bat` 裡放中文，連 `REM` 註解都不行。**

CMD 是以系統 ANSI 碼頁（繁中 Windows 為 cp950）逐行讀取批次檔。UTF-8 的中文位元組被當成 cp950 解讀後，可能解出 `&`、`|` 之類的命令分隔字元 —— 而 **`REM` 擋不住這些字元**，它們會破壞解析器，讓後面的 `if` / `echo` 區塊發生難以察覺的錯誤行為（實測曾出現 `if not exist` 條件不成立卻仍印出錯誤訊息）。

專案原本的 `launch_AI_Document_V3.bat` 在 REM 裡有中文卻能運作，只是因為那些中文misdecode 後剛好沒產生分隔字元 —— **那是運氣，不是設計**。

中文說明一律寫在這份 `.md`，`.bat` 只放英文。

> 同類問題已在 `backend/alembic.ini` 修過一次：該檔註解裡的破折號 `—` 觸發 `cp950 codec can't decode`，導致 Alembic 在所有繁中 Windows 上靜默失效、退回 legacy schema 路徑，`0002_chunk_section_path` 與 `0003_conversations` 兩個 migration 從未被套用。

---

## 已知的資安待辦

| 項目 | 嚴重度 | 狀態 |
|---|---|---|
| `pdfjs-dist` 開啟惡意 PDF 可執行任意 JS | high | **無上游修補**。修補版 `6.2.108`，但 `react-pdf@10.4.1`（最新）鎖死 `pdfjs-dist 5.4.296`。需評估 `overrides` 強制覆寫或執行期緩解。 |
| `rehype-raw` 直接渲染 LLM 輸出的原始 HTML | 高 | 未處理。`DocumentDetail.jsx` / `PdfPreviewModal.jsx` / `NotebookPage.jsx` 共 5 處。`package.json` 已有 `dompurify` 但從未被 import。roadmap 第 3 階段有列「LLM 輸出過 DOMPurify」。 |
| `react-router` open redirect | moderate | 需跨 major 升到 v7。 |
| `vite` / `esbuild` dev server | high / moderate | 正式模式下不成立；開發模式已限制為本機。根治需升 vite 至 7/8（breaking）。 |

`npm audit` 目前為 5 項（2 high、3 moderate），由 19 項降下來。
（後續把 pdfjs-dist 鎖回 5.4.296 後降為 4 項。）

---

## 已知問題（2026-08-12）

### 引用編號歸屬錯誤（LLM 行為，暫不處理）

**症狀**：答案內容正確，但 `[來源N]` 指向錯的來源。實測「buying mode有幾種」：
四個條目全標 `[來源4]`（第 11 頁的類別代號表），實際內容在來源 2
（第 13 頁起的切片，內容延伸到第 14 頁的 BUYING MODE 投影片）。
純 RAG 與混合模式行為一致。

**已排除的假設**：
- 不是後端資料錯亂 —— contexts 與 used_sources 在同一迴圈建立，編號對齊
- 不是「模型挑相似度最高的」—— 提示詞只含 `[來源N] 標題 (第X頁)`，模型看不到分數

**實際機制**：模型沒有做逐段歸屬，只是挑一個編號全部套用（四個條目
一字不差全標同號）。7B 模型在多來源歸屬上本來就不可靠。

**規劃中的解法**：答案生成後做「事後校驗」—— 把每個 [來源N] 段落的
文字與該來源實際內容比對（關鍵詞重疊），不符就改標或移除。此法只動
引用標記、不碰內容產出（內容不穩定的根因已確認是 Ollama 並行干擾，
已由序列化解決），風險低。動手前先在 golden set 加入「引用正確率」。

**注意**：來源清單的顯示順序是 cross-encoder 重排後的順序，但顯示的
分數是原始向量相似度 —— 看起來像亂序，不是 bug。

### VL 多頁分析佔住 LLM 名額（已緩解，未根治）

VL 分析是單一請求送出全部頁面，Ollama prefill 多張圖片期間毫無輸出，
後端無從偵測使用者已關閉視窗（前端有正確 abort，但斷線要等下一次寫入
才被發現）。名額被佔住時，後續查詢會排隊。

已緩解：等待上限 90 秒，超時放行。根治方向：VL 改逐頁呼叫（每頁之間
可偵測斷線、釋放名額），代價是答案無法跨頁綜合。
