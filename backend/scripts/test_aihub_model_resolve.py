"""AiHub 模型代號解析 —— 不需伺服器、不連線。

切換 LLM 後端時，模型 ID 必須跟著換命名體系：Ollama 用 tag（qwen3:8b），
AiHub 用 service/version（local/gpt-oss）。舊值若被一起存進 system_configs，
之後每一次查詢都會 500，而管理介面只顯示「LLM 模型 qwen3:8b」——
看起來像「切了但沒切成功」，完全對不上真正的原因。

這裡守的是：resolve_model 必須明確拒絕不認得的名稱（讓 admin 端點能在
存檔當下回 400），而不是猜一個服務商送出去。

執行：cd backend && .venv\\Scripts\\python.exe scripts/test_aihub_model_resolve.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.services.llm_provider.aihub_provider import (  # noqa: E402
    _MODEL_ALIASES, resolve_model,
)

FAIL = 0


def check(name, got, want):
    global FAIL
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL += 1


def rejects(model):
    try:
        resolve_model(model)
        return False
    except ValueError:
        return True


# 簡寫都要解析得出來
check("gpt-oss", resolve_model("gpt-oss"), ("local", "gpt-oss"))
check("gpt41", resolve_model("gpt41"), ("azure", "gpt41"))
check("claude45", resolve_model("claude45"), ("amazon", "claude45"))
check("大小寫不敏感", resolve_model("GPT-OSS"), ("local", "gpt-oss"))
check("前後空白", resolve_model("  gpt41-mini  "), ("azure", "gpt41-mini"))

# service/version 直接寫也要收
check("完整寫法", resolve_model("local/gpt-oss"), ("local", "gpt-oss"))
check("未登錄的完整寫法照收", resolve_model("azure/future-model"),
      ("azure", "future-model"))

# 關鍵：Ollama 的 tag 一定要被拒絕，不能猜
for m in ("qwen3:8b", "gemma4:12b", "llama3.1:8b", "bge-m3:latest"):
    check(f"拒絕 Ollama tag「{m}」", rejects(m), True)
check("拒絕空白亂填", rejects("does-not-exist"), True)

# 錯誤訊息要能指引使用者（admin 端點會把它原樣回給前端）
try:
    resolve_model("qwen3:8b")
    msg = ""
except ValueError as exc:
    msg = str(exc)
check("錯誤訊息含原輸入", "qwen3:8b" in msg, True)
check("錯誤訊息含 service/version 提示", "service/version" in msg, True)
check("錯誤訊息列出可用簡寫", "gpt-oss" in msg, True)

# 每個登錄的簡寫都必須真的解析得出來（防止清單與實作脫節）
bad = [k for k in _MODEL_ALIASES if rejects(k)]
check("所有登錄簡寫都可解析", bad, [])

print()
if FAIL:
    print(f"共 {FAIL} 項失敗")
    sys.exit(1)
print("全部通過")
