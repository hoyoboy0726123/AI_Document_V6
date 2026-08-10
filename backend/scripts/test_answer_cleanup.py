# -*- coding: utf-8 -*-
"""驗證答案清理：段落級去重 + 簡體轉正體。

去重的風險是誤刪。規範文件本來就有大量「相似但不同」的條目
（不同方法的同名程序、只差一個數值的兩列），刪錯比留著重複更糟，
所以測試重點放在「差一個字就必須保留」。
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.services.ollama_client import dedupe_sections, to_traditional  # noqa: E402

results = []


def check(name, ok, extra=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{('  ' + extra) if extra else ''}")


# ── 段落級去重 ──────────────────────────────────────────
DUP = """回答：以下是衝擊測試項目。

### 7. 桌面處理衝擊測試
- **測試條件**：驗證裝備在桌面處理環境下的耐衝擊能力 [來源9]。
- **測試程序**：測試程序和衝擊條件應根據指定的條件進行 [來源9]。

### 8. 擺錘衝擊測試（重力衝擊）
- **測試條件**：驗證裝備在模擬碰撞條件下的結構完整性 [來源9]。
- **測試程序**：測試程序和衝擊條件應根據指定的條件進行 [來源9]。

### 9. 擺錘衝擊測試（重力衝擊）
- **測試條件**：驗證裝備在模擬碰撞條件下的結構完整性 [來源9]。
- **測試程序**：測試程序和衝擊條件應根據指定的條件進行 [來源9]。

### 10. 擺錘衝擊測試（重力衝擊）
- **測試條件**：驗證裝備在模擬碰撞條件下的結構完整性 [來源9]。
- **測試程序**：測試程序和衝擊條件應根據指定的條件進行 [來源9]。
"""
out = dedupe_sections(DUP)
check("完全重複的段落只留一份", out.count("擺錘衝擊測試（重力衝擊）") == 1,
      f"→ {out.count('擺錘衝擊測試（重力衝擊）')} 次")
check("不同的段落保留", "桌面處理衝擊測試" in out)
check("段落外的文字保留", "回答：以下是衝擊測試項目。" in out)

# ── 不可誤刪：相似但不同 ──────────────────────────────
SIMILAR = """### 1. 功能衝擊測試
- **加速度**：40 g，持續 11 ms。

### 2. 運輸衝擊測試
- **加速度**：40 g，持續 23 ms。

### 3. 墜落測試
- **加速度**：40 g，持續 11 ms。
"""
out2 = dedupe_sections(SIMILAR)
check("只差一個數值的段落必須全部保留",
      all(k in out2 for k in ("功能衝擊測試", "運輸衝擊測試", "墜落測試")))
check("標題不同、內文相同也要保留（第1與第3項）", out2.count("40 g，持續 11 ms") == 2,
      f"→ {out2.count('40 g，持續 11 ms')} 次")

check("段落數少於 3 不處理", dedupe_sections("### A\nx\n\n### A\nx\n") == "### A\nx\n\n### A\nx\n")
check("短文字不處理", dedupe_sections("很短的答案") == "很短的答案")
check("空輸入安全", dedupe_sections("") == "" and dedupe_sections(None) is None)

# ── 簡體轉正體（OpenCC s2tw）──────────────────────────────
check("摆 → 擺", to_traditional("摆錘衝擊測試") == "擺錘衝擊測試")
check("多字轉換", to_traditional("这个设备的数据") == "這個設備的數據")
check("內建表涵蓋不到的高頻字也要轉",
      to_traditional("测试该范围为负值并请参阅图表") == "測試該範圍為負值並請參閱圖表",
      f"→ {to_traditional('测试该范围为负值并请参阅图表')}")
check("正體字不動", to_traditional("環境數量報告項目準則") == "環境數量報告項目準則")
check("英文與數字不動", to_traditional("MIL-STD-810H 35 ±1 ºC") == "MIL-STD-810H 35 ±1 ºC")

# 一對多消歧：單字表做不到，這是換成 OpenCC 的主要理由
check("一對多：干 依上下文", to_traditional("干燥箱与干扰源") == "乾燥箱與干擾源",
      f"→ {to_traditional('干燥箱与干扰源')}")
check("一對多：发 依上下文", to_traditional("发生故障与头发") == "發生故障與頭髮",
      f"→ {to_traditional('发生故障与头发')}")

# 不可做詞彙替換：s2twp 會把測試術語改掉，必須確認我們沒用到它
check("測試程序不可變成「程式」", "程序" in to_traditional("测试程序和冲击条件"),
      f"→ {to_traditional('测试程序和冲击条件')}")
check("參數不可變成「引數」", "參數" in to_traditional("详细参数说明"),
      f"→ {to_traditional('详细参数说明')}")
check("濕度用「濕」不用「溼」", to_traditional("湿度应保持稳定") == "濕度應保持穩定",
      f"→ {to_traditional('湿度应保持稳定')}")

check("空輸入安全", to_traditional("") == "" and to_traditional(None) is None)

print()
print("全部通過 ✅" if all(results) else f"有 {results.count(False)} 項失敗 ❌")
sys.exit(0 if all(results) else 1)
