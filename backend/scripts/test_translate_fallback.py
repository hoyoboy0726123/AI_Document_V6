"""驗證「中文查詢 BM25 失效才觸發翻譯」的行為與各項防呆。

重點在確認：
  1. BM25 本來就有命中 → 完全不觸發翻譯（不付延遲、不改變原本排序）
  2. 中文查詢 0 命中 → 才觸發，且真的撈到東西
  3. 英文查詢 0 命中 → 不觸發（沒有中文，翻譯沒意義）
  4. 設定關閉 → 不觸發
  5. 翻譯拋錯 / 逾時 → 回傳空結果，維持純向量，絕不讓查詢壞掉
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

from app.core.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.services import hybrid_search as hs  # noqa: E402

results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{('  ' + extra) if extra else ''}")


def main() -> None:
    db = SessionLocal()
    calls = {"n": 0}
    real = hs._translate_for_keyword

    def counting(q: str) -> str:
        calls["n"] += 1
        return real(q)

    try:
        # 1) BM25 本來就有命中 → 不該觸發翻譯
        hs._translate_for_keyword = counting
        calls["n"] = 0
        rows = hs.keyword_search(db, "Method 502", 10)
        check("有命中時不觸發翻譯", calls["n"] == 0 and len(rows) > 0,
              f"命中 {len(rows)} 筆，翻譯呼叫 {calls['n']} 次")

        # 2) 英文查詢 0 命中 → 不該觸發（沒有中文）
        calls["n"] = 0
        hs.keyword_search(db, "zzzqqq nonexistent term xyz", 10)
        check("英文查詢不觸發翻譯", calls["n"] == 0, f"翻譯呼叫 {calls['n']} 次")

        # 3) 設定關閉 → 不該觸發
        old = settings.RAG_QUERY_TRANSLATE_FALLBACK
        settings.RAG_QUERY_TRANSLATE_FALLBACK = False
        calls["n"] = 0
        hs.keyword_search(db, "低溫測試的試驗程序", 10)
        check("設定關閉時不觸發", calls["n"] == 0, f"翻譯呼叫 {calls['n']} 次")
        settings.RAG_QUERY_TRANSLATE_FALLBACK = old

        # 4) 翻譯拋錯 → 不可讓查詢壞掉
        hs._translate_for_keyword = lambda q: (_ for _ in ()).throw(RuntimeError("boom"))
        rows = hs.keyword_search(db, "低溫測試的試驗程序", 10)
        check("翻譯拋錯時安全退回", rows == [], "回傳空結果，維持純向量")

        # 5) 翻譯逾時 → 不可拖住查詢
        settings.RAG_QUERY_TRANSLATE_TIMEOUT = 2
        hs._translate_for_keyword = lambda q: (time.sleep(30), "x")[1]
        t0 = time.perf_counter()
        rows = hs.keyword_search(db, "低溫測試的試驗程序", 10)
        dt = time.perf_counter() - t0
        check("翻譯逾時時中止", rows == [] and dt < 6, f"耗時 {dt:.1f}s（上限 2s）")

        # 6) 真實路徑：中文 0 命中 → 觸發並撈到結果
        hs._translate_for_keyword = real
        settings.RAG_QUERY_TRANSLATE_TIMEOUT = 15
        hs._TRANSLATE_CACHE.clear()
        t0 = time.perf_counter()
        rows = hs.keyword_search(db, "低溫測試的試驗程序", 10)
        dt = time.perf_counter() - t0
        check("中文 0 命中時觸發並撈到結果", len(rows) > 0, f"{len(rows)} 筆，耗時 {dt:.1f}s")

        # 7) 快取生效：第二次同樣查詢不應再呼叫 LLM
        hs._translate_for_keyword = counting
        calls["n"] = 0
        t0 = time.perf_counter()
        hs.keyword_search(db, "低溫測試的試驗程序", 10)
        dt2 = time.perf_counter() - t0
        check("翻譯結果有快取", dt2 < max(1.0, dt / 2), f"第二次 {dt2:.2f}s（第一次 {dt:.1f}s）")

    finally:
        hs._translate_for_keyword = real
        db.close()

    print()
    print("全部通過 ✅" if all(o for _, o in results) else "有失敗 ❌")


if __name__ == "__main__":
    main()
