"""驗證追問歷史的不對稱額度分配。

重點:追問真正銜接的是「最新那一輪」，額度必須優先給它；
超過上限時由舊到新捨棄，最新一輪永不被擠掉。
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from app.services.ai import (  # noqa: E402
    _TRUNC_NOTE, _VL_HISTORY_BUDGET_CHARS, _VL_HISTORY_LATEST_CHARS,
    _VL_HISTORY_OLDER_CHARS, _compact_vl_history,
)

results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{('  ' + extra) if extra else ''}")


def turn(q: str, n: int) -> dict:
    return {"question": q, "answer": f"[{q}]" + ("測試條件內容。" * n)}


def main() -> None:
    check("無歷史回空字串", _compact_vl_history(None) == "" and _compact_vl_history([]) == "")

    # 單輪短答案:不該被截斷
    one = _compact_vl_history([turn("Q1", 5)])
    check("短答案不截斷", _TRUNC_NOTE not in one, f"{len(one)} 字")

    # 三輪長答案:最新一輪額度最大
    hist = [turn("舊", 400), turn("次新", 400), turn("最新", 400)]
    out = _compact_vl_history(hist)
    i_new, i_old = out.find("[最新]"), out.find("[舊]")
    check("最新一輪一定在", i_new >= 0)
    seg_new = out[i_new:] if i_new >= 0 else ""
    check("最新一輪拿到較大額度",
          len(seg_new) > _VL_HISTORY_OLDER_CHARS + 100,
          f"最新段 {len(seg_new)} 字 > 舊輪上限 {_VL_HISTORY_OLDER_CHARS}")
    check("總量不超過上限", len(out) <= _VL_HISTORY_BUDGET_CHARS + _VL_HISTORY_LATEST_CHARS,
          f"{len(out)} 字")

    # 極端:每輪都超長 → 最新一輪必留；額度用完後由「舊到新」捨棄
    # （兩輪時 1800+400 仍在 2600 額度內，屬於正常保留；要三輪才會超額）
    huge = _compact_vl_history([turn("最舊", 2000), turn("次舊", 2000), turn("最新", 2000)])
    check("最新一輪超長時仍保留", "[最新]" in huge, f"總 {len(huge)} 字")
    check("超額時先捨棄最舊的", "[最舊]" not in huge,
          f"保留次舊={'[次舊]' in huge}")

    # 時序:輸出必須是「舊→新」的閱讀順序
    seq = _compact_vl_history([turn("第一", 5), turn("第二", 5), turn("第三", 5)])
    check("維持舊→新順序",
          seq.find("[第一]") < seq.find("[第二]") < seq.find("[第三]"))

    print()
    print("全部通過 ✅" if all(o for _, o in results) else "有失敗 ❌")


if __name__ == "__main__":
    main()
