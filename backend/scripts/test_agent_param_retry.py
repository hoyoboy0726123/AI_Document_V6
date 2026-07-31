"""驗證 agent 參數題補救迴圈的判斷邏輯。

重點不只是「該觸發時會觸發」，更要確認「不該觸發時不會誤觸發」——
否則每個問題都跑補救迴圈，只會讓查詢變慢。
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from app.services.agent import (  # noqa: E402
    _extract_param_terms, _has_heavy_repetition, _history_subject, _is_degenerate,
    _is_no_answer, _lacks_subject, _lacks_values, _wants_values,
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
        # 問「方法」在規範語境下等同要參數：一份沒有溫濕度與時間的測試方法無法執行
        ("濕度測試方法", True),
        ("鹽霧測試的目的是什麼", False),
        ("這份文件的適用範圍", False),
        ("為什麼要做濕度測試", False),
        # 關係題含「方法」二字，但走 KG 確定性路徑，深查數值無意義
        ("哪些方法引用了 MIL-STD-810H", False),
        ("MIL-STD-810H 取代了哪些規範", False),
    ]:
        check(f"問題意圖：{q[:18]}", _wants_values(q) is want, f"→ {_wants_values(q)}")

    # 2) 答案是否缺數值
    check("空表格答案判為不足", _lacks_values(VAGUE_ANSWER) is True)
    check("含實際數值的答案判為足夠", _lacks_values(GOOD_ANSWER) is False)
    # 敘述型答案本身沒有數值，_lacks_values 會（刻意）回 True——
    # 攔住誤觸發的是「問題有沒有在要數值」那一關，所以這裡驗組合條件。
    check("敘述型答案：單看答案算不足", _lacks_values(NO_VALUE_TOPIC) is True)
    check("敘述型答案不誤觸發補救迴圈",
          (_wants_values("鹽霧測試的目的是什麼") and _lacks_values(NO_VALUE_TOPIC)) is False,
          "（目的題不算在要數值 → 不進補救迴圈）")
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
        # 白名單一定會有漏網：實測「冷凝測試」不在名單內而被判成無主體，
        # 於是繼承了上一輪的「濕度」——使用者問的明明是另一項測試。
        ("冷凝測試方法與條件", False),
        ("結露試驗的條件", False),
        ("砂蝕測試的參數", False),
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

    # 3.8) 實測踩到的案例:答案「一個數值都沒有」卻沒被判為不足
    REAL_VAGUE = """根據可用段落，濕度測試的條件如下：
濕度測試的條件與持續時間需根據運輸、儲存與部署時的氣候、持續時間及測試樣品配置來確定。測試條件應選取最壞情況作為選擇測試與條件的基礎。[來源2]
濕度測試的條件與持續時間需根據運輸、儲存與部署時的氣候、持續時間及測試樣品配置來確定。測試條件應選取最壞情況作為選擇測試與條件的基礎。[來源2]
濕度測試的條件可參考表 507.6-I，該表包含三個地理類別的溫度與相對濕度條件。
濕度測試的條件與持續時間需根據運輸、儲存與部署時的氣候、持續時間及測試樣品配置來確定。測試條件應選取最壞情況作為選擇測試與條件的基礎。[來源2]"""
    check("零數值答案判為不足（實測案例）", _lacks_values(REAL_VAGUE) is True)
    check("重複同一句判為不足", _has_heavy_repetition(REAL_VAGUE) is True)
    check("不重複的答案不誤判", _has_heavy_repetition(GOOD_ANSWER) is False)
    for s in ("需根據運輸、儲存與部署時的氣候、持續時間及測試樣品配置來確定",
              "應選取最壞情況", "可參考表 507.6-I", "視情況而定"):
        from app.services.agent import _VAGUE_RE
        check(f"空話措辭涵蓋：{s[:14]}", bool(_VAGUE_RE.search(s)))

    # 4) 整體觸發條件（實際 agent 用的兩條路徑）
    def gate(q: str, a: str) -> bool:
        return (_wants_values(q) and _lacks_values(a)) or _is_degenerate(a)

    for q, a, want, note in [
        ("鹽霧測試的測試條件", VAGUE_ANSWER, True, "明確要數值 + 空表格"),
        ("鹽霧測試的測試條件", GOOD_ANSWER, False, "已有數值不重複查"),
        # 使用者實際踩到的起點：問「方法」不算在要數值，但答案退化 → 仍要自己深查
        ("濕度測試方法", REAL_VAGUE, True, "退化答案路徑"),
        ("我要測試條件", REAL_VAGUE, True, "明確要數值路徑"),
        # 反向：本來就沒有數值的正常敘述答案不得誤觸發
        ("鹽霧測試的目的是什麼", NO_VALUE_TOPIC, False, "目的題"),
        ("這份文件的範圍是什麼", NO_VALUE_TOPIC, False, "scope 題"),
        # 但「方法題」拿到純敘述（連一個參數都沒有）應該深查——這正是使用者
        # 被迫追問 3 次的起點，寧可多花一輪也不要把問題丟回給使用者。
        ("鹽霧測試方法", NO_VALUE_TOPIC, True, "方法題只拿到敘述 → 深查"),
    ]:
        check(f"觸發判斷：{q[:12]}／{note}", gate(q, a) is want, f"→ {gate(q, a)}")

    print()
    print("全部通過 ✅" if all(o for _, o in results) else "有失敗 ❌")


if __name__ == "__main__":
    main()
