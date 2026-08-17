"""雲端 provider 是否收到取樣參數 —— 不需伺服器、不呼叫任何模型。

守的是可重現性：本地路徑（_chat_with_ollama）早就把 temperature=0 + seed
送進 Ollama，雲端分支卻完全不帶 options，於是用服務端預設溫度。
同一題每次答案都不同，golden set 與 A/B 比較全部失去意義。

實測（AiHub gpt-oss，同一份 golden set 連跑兩次，其餘條件不變）：
    u10  9/9 → 7/9
    u14  引用標記從半形 [來源N] 變成全形【來源N】
    u01  答案長度 2021 → 1310 字

執行：cd backend && .venv\\Scripts\\python.exe scripts/test_provider_options.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import settings          # noqa: E402
from app.services import ai                   # noqa: E402

FAIL = 0


def check(name, got, want):
    global FAIL
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL += 1


class FakeProvider:
    """假 provider：只記錄收到什麼，不連線。"""
    name = "fake-cloud"

    def __init__(self):
        self.seen = None

    def chat(self, messages, *, model=None, format=None, options=None):
        self.seen = {"model": model, "format": format, "options": options}
        return "ok"


fake = FakeProvider()
# _chat_with_provider 是在函式內才 import 工廠的，所以這裡要先把模組載進來
# 才能替換掉它的 get_llm_provider（延遲 import 會取到我們換過的那一個）。
import app.services.llm_provider as _factory_mod   # noqa: E402
orig_factory = _factory_mod.get_llm_provider
_factory_mod.get_llm_provider = lambda *a, **k: fake
try:
    saved = settings.OLLAMA_TEMPERATURE

    settings.OLLAMA_TEMPERATURE = 0.0
    ai._chat_with_provider([{"role": "user", "content": "x"}])
    check("temperature=0 有送到雲端 provider",
          (fake.seen or {}).get("options"), {"temperature": 0.0})

    settings.OLLAMA_TEMPERATURE = 0.7
    ai._chat_with_provider([{"role": "user", "content": "x"}])
    check("非 0 的溫度也照送",
          (fake.seen or {}).get("options"), {"temperature": 0.7})

    # 未設定時不要硬塞一個空 dict，讓 provider 用自己的預設
    settings.OLLAMA_TEMPERATURE = None
    ai._chat_with_provider([{"role": "user", "content": "x"}])
    check("未設定溫度時不傳 options", (fake.seen or {}).get("options"), None)

    settings.OLLAMA_TEMPERATURE = saved
finally:
    _factory_mod.get_llm_provider = orig_factory

# 每個 provider 的 chat 都必須收得下 options，否則上面的傳遞會 TypeError
import inspect  # noqa: E402
from app.services.llm_provider.base import LLMProvider          # noqa: E402
from app.services.llm_provider.ollama_provider import OllamaProvider  # noqa: E402
from app.services.llm_provider.gemini_provider import GeminiProvider  # noqa: E402
from app.services.llm_provider.aihub_provider import AiHubProvider    # noqa: E402

for cls in (LLMProvider, OllamaProvider, GeminiProvider, AiHubProvider):
    params = inspect.signature(cls.chat).parameters
    check(f"{cls.__name__}.chat 收得下 options", "options" in params, True)

print()
if FAIL:
    print(f"共 {FAIL} 項失敗")
    sys.exit(1)
print("全部通過")
