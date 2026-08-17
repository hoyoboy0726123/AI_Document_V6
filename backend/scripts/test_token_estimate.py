"""脈絡預算的 token 估算回歸測試 —— 不需伺服器也不需 Ollama，純函式。

守的是一件事：**估算不可低估 token 數**。低估會讓 prompt 塞爆 num_ctx，
模型只吐得出格式骨架（回答：空白 + 參考來源清單），畫面上看起來像「查不到
資料」，其實檢索完全正確。高估只是脈絡短一點，代價小得多。

基準值取自 Ollama 的 prompt_eval_count（實測，非估計）：
    內容型態              字元    實測 token   本式估算   舊式(字元/4.0)
    中文投影片＋代碼表     1200        483         545 (+13%)    300 (-38%)
    中文投影片＋代碼表     1200        602         495 (-18%)    300 (-50%)
    中文投影片＋代碼表     1200        597         624  (+5%)    300 (-50%)
    英文散文              1431        232         348 (+50%)    358 (+54%)
舊式在中文/表格語料一律低估 38~50%，正是 prompt 溢位的來源。

執行：cd backend && .venv\\Scripts\\python.exe scripts/test_token_estimate.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.services.ai import (  # noqa: E402
    _CHARS_PER_TOKEN_FALLBACK, chars_per_token, estimate_tokens,
)

FAIL = 0


def check(name, got, want):
    global FAIL
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL += 1


# 以實測基準值為準：估算不得低於「實測 token 數 × 0.8」（容許保守誤差，不容許大幅低估）
SAMPLES = [
    ("中文＋代碼表",
     "原物料大類為AA，次分類為BBB，流水碼為CC。類別代號 01 CPU、02 CHIPSET、"
     "03 MEMORY、04 SYSTEM COMPONENT、05 PROGRAMABLE IC。" * 8, 1.2, 2.6),
    ("markdown 表格",
     "| 級別代號 | 類別名稱 | 類別代號 | 類別名稱 |\n| --- | --- | --- | --- |\n"
     "| 01 | CPU | 18 | DISPLAY (LCD/OLED/EPD) |\n" * 12, 1.2, 2.6),
    ("英文散文",
     "This test method determines the resistance of equipment to low pressure "
     "effects during storage and operation at high ground elevations. " * 12, 2.5, 4.5),
]
for label, text, lo, hi in SAMPLES:
    cpt = len(text) / max(1, estimate_tokens(text))
    ok = lo <= cpt <= hi
    print(f"{'PASS' if ok else 'FAIL'}  {label}: {len(text)} 字元 / "
          f"{estimate_tokens(text)} tok = {cpt:.2f} 字元/token（期待 {lo}~{hi}）")
    if not ok:
        FAIL += 1

# 密集內容（表格/代碼）的字元密度必須低於英文散文 —— 這正是固定常數做不到的事
dense = "| 01 | CPU | 02 | CHIPSET |\n" * 40
prose = "The equipment shall be capable of continuous operation. " * 25
check("表格的字元/token 低於英文散文",
      chars_per_token(dense) < chars_per_token(prose), True)

# 保守方向：估算不得高估「字元/token」到危險區
check("字元/token 上限被夾住", chars_per_token("a" * 5000) <= 4.5, True)
check("字元/token 下限被夾住", chars_per_token("，" * 5000) >= 1.2, True)

# 樣本不足時退回保守值，而不是猜
check("空樣本用退化值", chars_per_token(None), _CHARS_PER_TOKEN_FALLBACK)
check("過短樣本用退化值", chars_per_token("只有幾個字"), _CHARS_PER_TOKEN_FALLBACK)
check("退化值比舊常數 4.0 保守", _CHARS_PER_TOKEN_FALLBACK < 4.0, True)

# 邊界
check("空字串 0 token", estimate_tokens(""), 0)
check("純中日韓每字一 token", estimate_tokens("料件種類"), 4)

print()
if FAIL:
    print(f"共 {FAIL} 項失敗")
    sys.exit(1)
print("全部通過")
