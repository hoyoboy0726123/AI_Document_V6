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
