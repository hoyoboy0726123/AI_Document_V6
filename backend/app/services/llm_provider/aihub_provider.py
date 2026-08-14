"""
ASUS AiHub OpenAPI implementation of LLMProvider（企業內部雲端模型閘道）。

與 Ollama/Gemini 的差異，以及本實作為什麼長這樣：

1. **三段式流程**：`GET /auth`（API KEY → token，30 天有效）→
   `POST /new_session`（取 session_id）→ `POST /chat`。token 與 session 都要快取，
   否則每題多付兩次往返（實測 new_session 平均 1.55 秒，佔 RAG 單題耗時的 10-30%）。

2. **session 有狀態，但對 RAG 無害**：實測同一 session 連問 12 輪各自獨立的
   RAG prompt（每輪不同方法、不同數值），零污染 —— 因為我們的 prompt 每次都自足
   （系統提示＋全部來源＋問題），模型沒有理由回頭翻歷史。因此採「session 池 +
   用滿 N 次才換」而非每次開新的。仍保留輪替上限當保險。

3. **沒有 messages 概念**：API 只吃單一 `message` 字串，所以 system/user 多段
   訊息要在這裡攤平。順序保持原樣，system 內容放最前面。

4. **不提供 embedding**：AiHub 沒有 embedding 端點。embed() 直接拋錯，
   工廠也不會把它建成 embedding provider（維持 Ollama 負責向量）。

5. **模型選擇是 service+version 兩個欄位**，不是單一 model 字串。這裡用
   "service/version" 的複合寫法（如 "local/gpt-oss"）對應既有的單一 model 參數，
   讓 admin 介面與設定檔不必為它新增欄位。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Iterator, List, Optional

import httpx

logger = logging.getLogger(__name__)

# model 字串 → (service, version)。允許使用者直接填 "local/gpt-oss"，
# 也接受這裡登錄的簡寫。清單取自 AiHub OpenAPI v0.9 文件。
_MODEL_ALIASES: Dict[str, tuple] = {
    "gpt-oss": ("local", "gpt-oss"),
    "gpt-oss:120b": ("local", "gpt-oss"),
    "gpt41": ("azure", "gpt41"),
    "gpt4.1": ("azure", "gpt41"),
    "gpt41-mini": ("azure", "gpt41-mini"),
    "gemini2.0": ("google", "gemini2.0"),
    "gemini2.5flash": ("google", "gemini2.5flash"),
    "gemini2.5pro": ("google", "gemini2.5pro"),
    "claude35": ("amazon", "claude35"),
    "claude37": ("amazon", "claude37"),
    "claude45": ("amazon", "claude45"),
}
_DEFAULT_MODEL = "gpt-oss"


def resolve_model(model: str) -> tuple:
    """把 model 字串解析成 (service, version)。"""
    m = (model or _DEFAULT_MODEL).strip()
    if "/" in m:
        svc, _, ver = m.partition("/")
        return svc.strip(), ver.strip()
    hit = _MODEL_ALIASES.get(m.lower())
    if hit:
        return hit
    # 未登錄的名稱不猜服務商，讓 API 回錯比靜默送錯模型好
    raise ValueError(
        f"AiHub 不認得模型「{m}」。請用 'service/version'（如 local/gpt-oss），"
        f"或這些簡寫之一：{', '.join(sorted(_MODEL_ALIASES))}"
    )


class AiHubProvider:
    name = "aihub"

    def __init__(self, api_key: str, base_url: str, *,
                 default_model: Optional[str] = None,
                 timeout: int = 600,
                 session_pool_size: int = 4,
                 session_max_uses: int = 200):
        if not api_key:
            raise RuntimeError("AIHUB_API_KEY 為空 — 請在 .env 設定或於 admin 頁面填入")
        self._api_key = api_key
        self._base = base_url.rstrip("/")
        self._default_model = default_model or _DEFAULT_MODEL
        self._timeout = timeout
        self._client = httpx.Client(timeout=timeout)

        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._token_at: float = 0.0
        # session 池：[(session_id, 已用次數)]，借出時從尾端取
        self._pool: List[List[Any]] = []
        self._pool_size = max(1, session_pool_size)
        self._max_uses = max(1, session_max_uses)

    # ───── 認證與 session ─────────────────────────────────────────
    def _get_token(self, force: bool = False) -> str:
        # token 文件寫 30 天有效，但另一段寫「24 小時內有效」——以較短者為準，
        # 保守用 12 小時就換，避免跨夜長跑到一半失效。
        with self._lock:
            if (not force and self._token
                    and time.time() - self._token_at < 12 * 3600):
                return self._token
        r = self._client.get(f"{self._base}/auth",
                             headers={"Authorization": self._api_key})
        r.raise_for_status()
        tok = r.json().get("token")
        if not tok:
            raise RuntimeError(f"AiHub auth 未回傳 token：{r.text[:200]}")
        with self._lock:
            self._token, self._token_at = tok, time.time()
            self._pool.clear()   # 換 token 後舊 session 一律作廢
        return tok

    def _new_session(self, token: str) -> str:
        r = self._client.post(f"{self._base}/new_session",
                              headers={"Authorization": token})
        r.raise_for_status()
        sid = r.json().get("session_id")
        if not sid:
            raise RuntimeError(f"AiHub new_session 未回傳 session_id：{r.text[:200]}")
        return sid

    def _acquire(self, token: str) -> List[Any]:
        with self._lock:
            while self._pool:
                entry = self._pool.pop()
                if entry[1] < self._max_uses:
                    return entry
                # 用滿了就丟掉，讓下面建新的
        return [self._new_session(token), 0]

    def _release(self, entry: List[Any]) -> None:
        entry[1] += 1
        with self._lock:
            if len(self._pool) < self._pool_size and entry[1] < self._max_uses:
                self._pool.append(entry)

    # ───── LLMProvider ────────────────────────────────────────────
    @staticmethod
    def _flatten(messages: List[Dict[str, Any]]) -> str:
        """把 messages 攤平成單一字串（AiHub 只吃一個 message 欄位）。"""
        parts: List[str] = []
        for m in messages or []:
            content = (m.get("content") or "").strip()
            if not content:
                continue
            parts.append(content)
        return "\n\n".join(parts)

    def chat(self, messages: List[Dict[str, Any]], *,
             model: Optional[str] = None,
             format: Optional[Any] = None,
             options: Optional[Dict[str, Any]] = None) -> str:
        service, version = resolve_model(model or self._default_model)
        payload = {
            "session_id": None,       # 下面填
            "response_type": "normal",  # 文件 v0.9：streaming 已不支援
            "assistant_id": "",
            "service": service,
            "version": version,
            "message": self._flatten(messages),
        }
        temp = (options or {}).get("temperature")
        if temp is not None:
            payload["temperature"] = temp

        # token 失效（401）時重取一次再試；其餘錯誤直接往上拋。
        for attempt in (1, 2):
            token = self._get_token(force=(attempt == 2))
            entry = self._acquire(token)
            payload["session_id"] = entry[0]
            try:
                r = self._client.post(f"{self._base}/chat",
                                      headers={"Authorization": token},
                                      json=payload)
            except Exception:
                # 連線層失敗不把 session 放回池（狀態未知）
                raise
            if r.status_code == 401 and attempt == 1:
                logger.info("AiHub token 失效，重新認證後重試")
                continue
            self._release(entry)
            if r.status_code != 200:
                raise RuntimeError(f"AiHub chat 失敗 HTTP {r.status_code}：{r.text[:300]}")
            data = r.json()
            err = data.get("error")
            if err and str(err).lower() not in ("null", "none", ""):
                raise RuntimeError(f"AiHub chat 回報錯誤：{err}")
            return data.get("textResponse") or ""
        raise RuntimeError("AiHub chat 重試後仍失敗")

    def chat_stream(self, messages: List[Dict[str, Any]], *,
                    model: Optional[str] = None,
                    options: Optional[Dict[str, Any]] = None) -> Iterator[Dict[str, str]]:
        """AiHub v0.9 已移除 streaming；一次取回後以單一 chunk 回吐，
        讓呼叫端（SSE 端點）不必分支。"""
        text = self.chat(messages, model=model, options=options)
        yield {"message": {"content": text}, "done": False}
        yield {"message": {"content": ""}, "done": True}

    def generate(self, prompt: str, *,
                 model: Optional[str] = None,
                 images: Optional[List[str]] = None,
                 system: Optional[str] = None,
                 format: Optional[Any] = None,
                 options: Optional[Dict[str, Any]] = None) -> str:
        if images:
            raise RuntimeError("AiHub OpenAPI 不支援影像輸入（VL 請維持本地 Ollama）")
        msgs: List[Dict[str, Any]] = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": prompt})
        return self.chat(msgs, model=model, format=format, options=options)

    def embed(self, inputs: List[str], *, model: Optional[str] = None) -> List[List[float]]:
        raise RuntimeError(
            "AiHub OpenAPI 沒有 embedding 端點 —— 向量請維持 EMBEDDING_PROVIDER=ollama"
        )

    def list_models(self) -> List[Dict[str, Any]]:
        return [{"name": k} for k in sorted(_MODEL_ALIASES)]

    def version(self) -> Optional[str]:
        return "aihub-openapi-v0.9"
