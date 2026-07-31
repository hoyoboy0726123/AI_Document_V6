"""驗證「補查預算」在真實 SSE 傳輸路徑下仍然有效。

為什麼要有這支：原本預算用 ContextVar 存放，單元測試同步驅動 generator 時
一切正常，但 run_agent 是經 StreamingResponse 回傳的 generator，Starlette 的
iterate_in_threadpool 對每次 next() 都用 copy_context() 執行 —— generator 內
set() 的 ContextVar 在第一次 yield 之後就消失。

結果是每個合成點都取到 None、各自重新拿一份完整額度。run_agent 有 6 個合成
呼叫點，最壞情況 6 x (1+3) = 24 次 LLM 生成，也就是「單一請求卡住數分鐘」
那個以為已經修好的病。

所以任何「跨 yield 的狀態」都必須用這條路徑驗證，不能只跑同步測試。
"""
from __future__ import annotations

import asyncio
import contextvars
import sys

sys.path.insert(0, ".")

from starlette.concurrency import iterate_in_threadpool  # noqa: E402

from app.services.agent import _new_retry_budget  # noqa: E402

results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{('  ' + extra) if extra else ''}")


# 兩個檢查各用獨立的 ContextVar。
# 同一個變數不行：同步測試會在主執行緒的 context 裡留下值，之後 copy_context()
# 複製到的就是那個已有值的 context，於是串流測試看起來「正常」——測試自己把
# 要驗的現象掩蓋掉了。這個坑第一次就踩到了。
_CV_SYNC: contextvars.ContextVar = contextvars.ContextVar("demo_sync", default=None)
_CV_SSE: contextvars.ContextVar = contextvars.ContextVar("demo_sse", default=None)


def _cv_generator(log: list, cv: contextvars.ContextVar):
    """示範用：把狀態放進 ContextVar（這是壞掉的做法）。"""
    cv.set([3])
    log.append(cv.get() is not None)
    yield "a"
    log.append(cv.get() is not None)
    yield "b"
    log.append(cv.get() is not None)
    yield "c"


def _param_generator(budget: list, log: list):
    """正確做法：狀態放在區域可變物件，沿呼叫鏈傳遞。"""
    log.append(budget[0])
    yield "a"
    budget[0] -= 1
    log.append(budget[0])
    yield "b"
    budget[0] -= 1
    log.append(budget[0])
    yield "c"


def drive_streaming(gen) -> None:
    """用與 StreamingResponse 相同的方式驅動 generator。"""
    async def _run():
        async for _ in iterate_in_threadpool(gen):
            pass
    asyncio.run(_run())


def main() -> None:
    # 1) 先證明「同步驅動驗不出來」——這正是原本測試漏掉它的原因
    sync_log: list = []
    for _ in _cv_generator(sync_log, _CV_SYNC):
        pass
    check("同步驅動時 ContextVar 看起來正常（所以單元測試驗不出問題）",
          sync_log == [True, True, True], f"→ {sync_log}")

    # 2) 真實 SSE 路徑下 ContextVar 會消失
    sse_log: list = []
    drive_streaming(_cv_generator(sse_log, _CV_SSE))
    check("經 iterate_in_threadpool 時 ContextVar 在 yield 後消失",
          sse_log[0] is True and sse_log[1:] == [False, False], f"→ {sse_log}")

    # 3) 改用參數傳遞後，跨 yield 的狀態才真的守得住
    budget = _new_retry_budget()
    param_log: list = []
    drive_streaming(_param_generator(budget, param_log))
    check("改用可變物件傳遞後，跨 yield 狀態保留", param_log == [3, 2, 1],
          f"→ {param_log}")
    check("預算確實被扣減（不是每次重拿新的）", budget[0] == 1, f"→ {budget[0]}")

    # 4) 每個請求各自獨立，互不干擾
    a, b = _new_retry_budget(), _new_retry_budget()
    a[0] -= 2
    check("不同請求的預算互相獨立", a[0] == 1 and b[0] == 3, f"→ a={a[0]} b={b[0]}")

    print()
    print("全部通過 ✅" if all(o for _, o in results) else "有失敗 ❌")


if __name__ == "__main__":
    main()
