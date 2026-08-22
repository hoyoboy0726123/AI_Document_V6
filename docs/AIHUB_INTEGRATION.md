# ASUS AiHub OpenAPI 接入技術指引

> 供其他專案接入 AiHub 雲端模型用。內容全部來自 AI_Document V6/V7 的實際接入經驗,
> 「坑」章節的每一條都有實測依據,不是文件轉述。
> 依據:AiHub OpenAPI **v0.9** 文件 + 2026/08 實測。

---

## 1. 三段式流程總覽

```
GET  /auth          API KEY ──→ token          (效期見坑 #6,建議快取 12 小時)
POST /new_session   token   ──→ session_id     (~1.55 秒,建議池化重用)
POST /chat          token + session_id + 訊息 ──→ 整段回覆(無串流)
```

| 環境 | Base URL |
|---|---|
| 測試區 | `https://stage-iotapi.asus.com/aoccgpt2/v1/openapi` |
| 正式區 | `https://aoccaihub.asus.com/aoccgpt2/v1/openapi` |

### 各端點細節

**`GET /auth`** — Header `Authorization: <API_KEY>`,回 `{"token": "..."}`。

**`POST /new_session`** — Header `Authorization: <token>`,回 `{"session_id": "..."}`。

**`POST /chat`** — Header `Authorization: <token>`,body:

```json
{
  "session_id": "<session_id>",
  "response_type": "normal",
  "assistant_id": "",
  "service": "local",
  "version": "gpt-oss",
  "message": "你的完整提示詞(單一字串)",
  "temperature": 0.2
}
```

回應:`{"textResponse": "...", "error": null}`。

---

## 2. 模型選擇(service + version 兩欄位)

| 簡寫 | service/version | 資料去向 | 備註 |
|---|---|---|---|
| `gpt-oss` | `local/gpt-oss` | **華碩自建(120B)** | **機密資料唯一選擇** |
| `gpt41` / `gpt41-mini` | `azure/gpt41(-mini)` | 微軟 Azure | 資料出外部廠商 |
| `gemini2.5pro` / `gemini2.5flash` / `gemini2.0` | `google/...` | Google | 資料出外部廠商 |
| `claude45` / `claude37` / `claude35` | `amazon/...` | AWS Bedrock | 資料出外部廠商 |

> ⚠️ **資安分級是接入方的責任**:只有 `local/gpt-oss` 是自建部署;其餘 alias 的
> 請求內容都會送到外部雲端廠商。內部文件、機密語料一律鎖 `gpt-oss`,
> 建議在程式層寫死白名單,不要讓使用者自由選。

> 未登錄的模型名稱**不要猜服務商** —— 直接拋錯讓呼叫端看到,
> 比靜默送錯模型好(我們的 `resolve_model()` 就是這樣做)。

---

## 3. 最小可用範例(Python / httpx)

```python
import httpx, time

BASE = "https://stage-iotapi.asus.com/aoccgpt2/v1/openapi"
API_KEY = "..."   # 一律從 .env 讀,絕不硬編碼、絕不進 DB

class AiHub:
    def __init__(self):
        self._c = httpx.Client(timeout=600)
        self._token, self._token_at = None, 0
        self._sessions = []          # 簡易 session 池

    def _get_token(self, force=False):
        if not force and self._token and time.time() - self._token_at < 12*3600:
            return self._token
        r = self._c.get(f"{BASE}/auth", headers={"Authorization": API_KEY})
        r.raise_for_status()
        self._token, self._token_at = r.json()["token"], time.time()
        self._sessions.clear()       # 換 token 後舊 session 一律作廢
        return self._token

    def _session(self, token):
        if self._sessions:
            return self._sessions.pop()
        r = self._c.post(f"{BASE}/new_session", headers={"Authorization": token})
        r.raise_for_status()
        return r.json()["session_id"]

    def chat(self, prompt, service="local", version="gpt-oss", temperature=0.2):
        for attempt in (1, 2):
            token = self._get_token(force=(attempt == 2))
            sid = self._session(token)
            r = self._c.post(f"{BASE}/chat", headers={"Authorization": token}, json={
                "session_id": sid, "response_type": "normal", "assistant_id": "",
                "service": service, "version": version,
                "message": prompt, "temperature": temperature,
            })
            if r.status_code == 401 and attempt == 1:
                continue             # token 失效 → 重新認證重試一次
            r.raise_for_status()
            data = r.json()
            err = data.get("error")
            if err and str(err).lower() not in ("null", "none", ""):
                raise RuntimeError(f"AiHub error: {err}")
            self._sessions.append(sid)   # 成功才放回池
            return data.get("textResponse") or ""
        raise RuntimeError("AiHub chat 重試後仍失敗")
```

---

## 4. 坑清單(每條都踩過,附實測)

### 坑 1:不支援 streaming —— 別浪費時間試
v0.9 文件已移除 streaming;實測把 `response_type` 改成串流模式,
**五個模型全部回同一個伺服器端錯誤**(`JSONEncoder ... 'intent'`)。
答案只能整段等(長答案 ~20 秒)。
**UX 對策**:檢索完先把來源推給前端(我們 ~2 秒先出來源),或做兩段式生成
(先短答案再補完整版),不要讓使用者盯白屏。

### 坑 2:temperature=0 收得下,但**沒有可重現性**
文件寫多數模型溫度下限 0.2(gpt41/gemini 系為 0~2)。實測送 0 **不會報錯**,
但同一提示詞連打兩次**輸出仍然不同**(閘道忽略或夾限,或 gpt-oss 本身 MoE 不確定)。
**後果**:所有建立在「temperature=0=可重現」上的評測/回歸比較都會被雜訊騙。
**對策**:單次評測不下結論;掉分先單項重跑;A/B 比較看分佈不看單點。

### 坑 3:沒有 JSON mode
沒有 `format=json` 這種保證。要結構化輸出就在提示詞裡要求「只輸出 JSON」,
並準備**寬鬆解析**:剝 ``` 圍籬 → 取第一個 `{` 到最後一個 `}` 切片 → `json.loads`。

### 坑 4:沒有 embedding 端點
向量化必須留在地端(我們用 Ollama + qwen3-embedding)。
接入層的 `embed()` 應直接拋錯,不要靜默退化。

### 坑 5:沒有 messages 概念 —— 只吃單一 `message` 字串
system/user 多段訊息要**自己攤平**(順序保留、system 放最前、`\n\n` 串接)。
多輪對話歷史也要攤進去 —— API 的 session 雖然有狀態,但別依賴它記歷史(見坑 7)。

### 坑 6:token 效期文件自相矛盾
文件一處寫 30 天、另一處寫 24 小時內有效。**以較短者為準再打折**:
我們快取 12 小時就換新;另外收到 401 時強制重新認證再重試一次(只重試一次)。
**換 token 後舊 session 一律作廢清池**。

### 坑 7:session 有狀態,但可以池化重用
`new_session` 平均 **1.55 秒**,每題都開新 session 等於白付 10-30% 延遲。
實測同一 session 連打 12 輪**各自獨立的自足 prompt**(每輪完整帶系統提示+全部
上下文),零污染 —— 因為 prompt 自足,模型沒理由回頭翻 session 歷史。
**對策**:session 池(我們 pool=4、單 session 用滿 200 次輪替)。
前提是你的 prompt 必須自足;若你依賴 session 記憶,就不能共用池。

### 坑 8:`error` 欄位可能是字串 "null"
判斷錯誤不能只寫 `if data.get("error")` —— 會把字串 `"null"`/`"None"` 當成錯誤。
照範例排除這幾個字串。

### 坑 9:延遲輪廓(規劃逾時用)
短輸出(關鍵詞/JSON 決策)~5-20 秒;長 RAG 答案 ~21 秒;偶發更久。
HTTP client timeout 建議 ≥600 秒,重試策略只針對 401,其餘錯誤直接上拋。

### 坑 10:任務適配 —— 大不等於好(實測)
在 OCR 中文語料上與地端 27B 對比:
- **GPT-OSS 擅長字面任務**:規範編號引用抽取 11/11 零幻覺
- **GPT-OSS 不擅長判斷/命名任務**:表格語意判斷、母節點命名明顯輸給地端
  27B(把螢幕截圖描述當知識抽、命名跟使用者提問對不上)
**對策**:別假設 120B 全面輾壓;判斷型任務先做小樣本 A/B,或保留人工審核閘門。

---

## 5. 接入檢核清單

- [ ] API KEY 只放 `.env`,不進程式碼、不進 DB、不進管理介面
- [ ] token 快取(≤12h)+ 401 單次重試 + 換 token 清 session 池
- [ ] session 池化(前提:prompt 自足)
- [ ] 機密專案:模型白名單鎖 `local/gpt-oss`
- [ ] 無串流的 UX 補償(先推部分結果/骨架畫面)
- [ ] JSON 輸出走寬鬆解析
- [ ] embedding 留地端
- [ ] 評測不假設可重現;單次結果不下結論
- [ ] 逾時 ≥600s;`error=="null"` 不當錯誤

---

*維護:AI_Document 專案組。實作參考 `backend/app/services/llm_provider/aihub_provider.py`。*
