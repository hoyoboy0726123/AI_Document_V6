"""驗證追問歷史的取捨策略:完整優先 → 丟最舊 → 截斷（最後手段）。

重點:
  1. 放得下就「完全不截斷」——即使有 3 輪
  2. 放不下時由最舊往新丟，最新一輪永不被丟
  3. 只剩最新一輪仍超額時，它獨享全部額度
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from app.services.ai import (  # noqa: E402
    _TRUNC_NOTE, _VL_HISTORY_BUDGET_CHARS, _compact_vl_history,
)

results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{('  ' + extra) if extra else ''}")


def turn(tag: str, chars: int) -> dict:
    """產生答案長度約 chars 的一輪對話。"""
    body = "測試條件內容。" * max(1, chars // 7)
    return {"question": f"問題{tag}", "answer": f"[{tag}]{body}"}


def main() -> None:
    B = _VL_HISTORY_BUDGET_CHARS
    check("無歷史回空字串", _compact_vl_history(None) == "" and _compact_vl_history([]) == "")

    # 1) 三輪都很短 → 全部完整帶入，不得出現截斷標記
    small = [turn("A", 300), turn("B", 300), turn("C", 300)]
    out = _compact_vl_history(small)
    check("三輪都放得下 → 全部完整", _TRUNC_NOTE not in out and all(f"[{t}]" in out for t in "ABC"),
          f"{len(out)} 字")

    # 2) 三輪超額 → 丟最舊，保留較新的且仍不截斷
    mid = [turn("A", 1200), turn("B", 1200), turn("C", 1200)]
    out = _compact_vl_history(mid)
    check("超額時丟掉最舊的", "[A]" not in out, f"保留 B={'[B]' in out} C={'[C]' in out}")
    check("被保留的輪次仍完整（未截斷）", _TRUNC_NOTE not in out, f"{len(out)} 字")
    check("最新一輪一定在", "[C]" in out)
    check("總量不超過額度", len(out) <= B, f"{len(out)} <= {B}")

    # 3) 每輪都超長 → 只剩最新一輪，且它獨享全部額度
    huge = [turn("A", 3000), turn("B", 3000), turn("C", 3000)]
    out = _compact_vl_history(huge)
    check("極端情況只留最新一輪", "[C]" in out and "[A]" not in out and "[B]" not in out)
    check("最新一輪獨享額度（用掉大部分）", len(out) > B * 0.9, f"{len(out)} 字 / 額度 {B}")
    check("超額時才出現截斷標記", _TRUNC_NOTE in out)
    check("截斷後仍不超額", len(out) <= B, f"{len(out)} <= {B}")

    # 4) 單輪短答案不該被動到
    one = _compact_vl_history([turn("Z", 200)])
    check("單輪短答案完整保留", _TRUNC_NOTE not in one and "[Z]" in one)

    # 5) 順序必須是舊→新
    seq = _compact_vl_history([turn("1", 200), turn("2", 200), turn("3", 200)])
    check("維持舊→新順序", seq.find("[1]") < seq.find("[2]") < seq.find("[3]"))

    print()
    print("全部通過 ✅" if all(o for _, o in results) else "有失敗 ❌")


if __name__ == "__main__":
    main()
