"""驗證 agent 參數題補救迴圈的判斷邏輯。

重點不只是「該觸發時會觸發」，更要確認「不該觸發時不會誤觸發」——
否則每個問題都跑補救迴圈，只會讓查詢變慢。
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from app.services.agent import (  # noqa: E402
    _extract_param_terms, _history_subject, _is_no_answer, _lacks_subject,
    _lacks_values, _wants_values,
)

results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{('  ' + extra) if extra else ''}")


# 使用者實際遇到的空表格答案
VAGUE_ANSWER = """鹽霧測試方法：
| 項目 | 要求 |
| 鹽溶液 pH | 需根據測試項目需求設定，並在測試前明確規定 |
| 鹽溶液降解率 | 需根據測試項目需求設定，並在測試前明確規定 |
| 暴露時間 | 需根據測試項目需求設定，並在測試前明確規定 |
測試至少持續 28 天以觀察真菌生長。"""

GOOD_ANSWER = """鹽霧測試條件：鹽溶液濃度 5 ±1% 重量百分比，比重 1.025 至 1.037，
測試溫度 33ºC 至 36ºC，噴霧沉降率 1 至 3 ml/cm2/hr，持續 48 小時。"""

NO_VALUE_TOPIC = """本方法用於評估裝備在鹽霧環境下的耐受性，適用於可能暴露於
海洋或沿海環境的裝備。不適用於評估腐蝕防護塗層的長期效能。"""


def main() -> None:
    # 1) 問題意圖判斷
    for q, want in [
        ("鹽霧測試的測試條件與時間", True),
        ("沙塵測試的塵土濃度是多少", True),
        ("低溫測試的溫度要求", True),
        ("what is the salt concentration", True),
        ("鹽霧測試的目的是什麼", False),
        ("哪些方法引用了 MIL-STD-810H", False),
    ]:
        check(f"問題意圖：{q[:18]}", _wants_values(q) is want, f"→ {_wants_values(q)}")

    # 2) 答案是否缺數值
    check("空表格答案判為不足", _lacks_values(VAGUE_ANSWER) is True)
    check("含實際數值的答案判為足夠", _lacks_values(GOOD_ANSWER) is False)
    check("敘述型答案不誤判", _lacks_values(NO_VALUE_TOPIC) is False,
          "（無空話措辭，不該進補救迴圈）")
    check("空答案判為不足", _lacks_values("") is True)

    # 3) 參數名稱抽取（下一輪查詢的種子）
    evidence = [{"snippet":
                 "3.1 Pretest. The following information is required.(a) Salt solution pH. "
                 "(b) Salt solution fallout rate (ml/cm2/hr). (c) Orientation of the test item."}]
    terms = _extract_param_terms(evidence, VAGUE_ANSWER)
    check("抽得到參數名稱", len(terms) >= 2, f"→ {terms}")
    check("抽出的是英文術語（與語料語言一致）",
          all(any(c.isascii() and c.isalpha() for c in t) for t in terms) if terms else False)

    # 3.5) 「直接放棄」的答案要被抓到（實測 agent 有時第 1 步就回查無相關資料）
    check("查無相關資料判為放棄", _is_no_answer("查無相關資料。\n參考來源：\n* [來源1]") is True)
    check("空答案判為放棄", _is_no_answer("") is True)
    check("正常答案不誤判為放棄", _is_no_answer(GOOD_ANSWER) is False)
    long_with_phrase = (
        "震動測試條件如下：頻率準確度 ±0.5 Hz（5 Hz 至 50 Hz）、±2 Hz（50 至 500 Hz），"
        "持續時間準確度為指定時間的 ±3%。隨機震動的頻率準確度維持在濾波器解析度的一半。"
        "另關於發火裝置的長期老化資料，本文件查無相關資料，需另行查閱其他規範。"
    )
    check("長答案中提到「查無」不整篇誤判", _is_no_answer(long_with_phrase) is False,
          "（僅短且放棄的答案才算）")

    # 3.6) 問題有沒有指名對象（決定是否改用反問）
    for q, want in [
        ("找出測試條件", True),
        ("測試條件是什麼", True),
        ("請問參數", True),
        ("震動測試的測試條件", False),
        ("鹽霧測試方法", False),
        ("MIL-STD-810H 有哪些程序", False),
        ("Method 514 的條件", False),
        ("低溫測試要幾度", False),
    ]:
        check(f"缺主體判斷：{q[:16]}", _lacks_subject(q) is want, f"→ {_lacks_subject(q)}")

    # 反問條件 = 缺主體 且 在要數值（不看模型有沒有勉強生出答案：
    # 實測它會隨機挑一項測試回答得像真的，那比誠實說查不到更危險）
    def clarifies(q: str) -> bool:
        return _lacks_subject(q) and _wants_values(q)

    for q, want in [
        ("找出測試條件", True),
        ("測試參數是多少", True),
        ("震動測試的測試條件", False),   # 有主體
        ("有哪些測試方法", False),        # 缺主體但不是在要數值
        ("這份文件的範圍是什麼", False),  # 同上
        ("Method 514 的條件", False),
    ]:
        check(f"是否該反問：{q[:16]}", clarifies(q) is want, f"→ {clarifies(q)}")

    # 3.7) 主體省略型追問要繼承上一輪的對象（而非反問或放棄）
    hist = [{"question": "振動測試的測試條件", "answer": "頻率準確度 ±0.5 Hz…"}]
    check("從歷史繼承主體", _history_subject(hist) in ("振動", "振動測試"),
          f"→ {_history_subject(hist)}")
    check("歷史無主體時回 None", _history_subject(
        [{"question": "這份文件多長", "answer": "約 1089 頁"}]) is None)
    check("無歷史時回 None", _history_subject(None) is None)
    check("繼承後的查詢含主體",
          not _lacks_subject(f"{_history_subject(hist)}的找出測試條件"))

    # 4) 整體觸發條件
    trigger = _wants_values("鹽霧測試的測試條件") and _lacks_values(VAGUE_ANSWER)
    check("該觸發時會觸發", trigger is True)
    no_trigger = _wants_values("鹽霧測試的測試條件") and _lacks_values(GOOD_ANSWER)
    check("已有數值時不重複查", no_trigger is False)

    print()
    print("全部通過 ✅" if all(o for _, o in results) else "有失敗 ❌")


if __name__ == "__main__":
    main()
