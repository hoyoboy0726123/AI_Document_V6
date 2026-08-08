# -*- coding: utf-8 -*-
"""驗證 _degrade_empty_tables：空表格降級、真表格不動。

重點是「不誤傷」——只要表格裡有任何一個真數值就必須原樣保留。
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.services.agent import _degrade_empty_tables  # noqa: E402

results = []


def check(name, ok, extra=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{('  ' + extra) if extra else ''}")


EMPTY = """回答：
沙塵測試的參數如下：

| 參數 | 數值 |
| --- | --- |
| 溫度 | 依測試計畫指定 |
| 濕度 | 依測試計畫指定 |
| 風速 | 依測試計畫指定 |
"""

REAL = """| 參數 | 數值 |
| --- | --- |
| 溫度 | 23 ± 2°C |
| 濕度 | 50 percent |
| 風速 | 4.6 m/s |
"""

MIXED = """| 參數 | 數值 |
| --- | --- |
| 溫度 | 依測試計畫指定 |
| 濕度 | 依測試計畫指定 |
| 風速 | 4.6 m/s |
"""

SHORT = """| 參數 | 數值 |
| --- | --- |
| 溫度 | 依測試計畫指定 |
| 濕度 | 依測試計畫指定 |
"""

DASHES = """| 項目 | 條件 | 備註 |
| --- | --- | --- |
| 高溫 | — | — |
| 低溫 | — | — |
| 溫變 | — | — |
"""

out = _degrade_empty_tables(EMPTY)
check("全佔位表格被降級為條列",
      "|" not in out and "- 溫度" in out and "須" not in out and "測試計畫" in out)
check("降級後保留表格外的敘述", "沙塵測試的參數如下：" in out)
check("含真數值的表格原樣保留", _degrade_empty_tables(REAL) == REAL)
check("只要有一個真數值就不降級", _degrade_empty_tables(MIXED) == MIXED)
check("資料列少於 3 列不降級（可能只是還沒填完）", _degrade_empty_tables(SHORT) == SHORT)
check("破折號佔位表格也被降級", "|" not in _degrade_empty_tables(DASHES))
check("無表格的答案不受影響", _degrade_empty_tables("純文字答案 23 ± 2°C") == "純文字答案 23 ± 2°C")
check("None / 空字串安全", _degrade_empty_tables(None) is None and _degrade_empty_tables("") == "")
check("冪等（重跑結果相同）", _degrade_empty_tables(out) == out)

print()
print("全部通過 ✅" if all(results) else f"有 {results.count(False)} 項失敗 ❌")
sys.exit(0 if all(results) else 1)
