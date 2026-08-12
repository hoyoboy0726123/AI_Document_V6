# -*- coding: utf-8 -*-
"""驗證短縮寫（2 字母）的 GLOB 補救檢索。

背景：FTS5 的 trigram tokenizer 需要 token ≥ 3 字元，所以「ER」「PR」這類
兩字母縮寫在 BM25 裡完全搜不到（實測 MATCH "ER" 回 0 塊）。縮寫查詢因此
只剩向量一條路，而向量對這種查詢特別不可靠 —— 定義常被埋在一個主要在講
別的事的大塊裡，相似度反而輸給語意相近的噪音。

測試重點放在「不可誤觸發」：這條路徑會做全表 GLOB 掃描（約 0.2-0.6 秒），
不含縮寫的查詢必須完全不進來，否則每次查詢都平白多付這個成本。
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.services.hybrid_search import (  # noqa: E402
    _ABBREV_SCORE, _SHORT_ABBREV_RE, _merge_abbrev,
)

results = []


def check(name, ok, extra=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{('  ' + extra) if extra else ''}")


def toks(q):
    return _SHORT_ABBREV_RE.findall(q)


# ── 該抓到的 ────────────────────────────────────────────
check("空白分隔的縮寫", toks("ER PR 是什麼") == ["ER", "PR"], f"→ {toks('ER PR 是什麼')}")
check("縮寫後面直接接中文（\\b 抓不到，靠 lookahead）",
      toks("ER PR是什麼?") == ["ER", "PR"], f"→ {toks('ER PR是什麼?')}")
check("句首與句尾", toks("PR") == ["PR"] and toks("什麼是 EC") == ["EC"])
check("括號與標點包夾", toks("試產（PR）階段") == ["PR"], f"→ {toks('試產（PR）階段')}")

# ── 不該抓到的（誤觸發會讓每次查詢多付全表掃描）──────────
check("小寫不是縮寫", toks("er pr is what") == [], f"→ {toks('er pr is what')}")
check("三字母以上交給 trigram（本來就搜得到）", toks("BOM EMS OEM") == [],
      f"→ {toks('BOM EMS OEM')}")
check("純中文查詢不觸發", toks("料件有哪些種類") == [])
check("規範編號不可被拆出兩字母",
      toks("MIL-STD-810H 沙塵測試") == [], f"→ {toks('MIL-STD-810H 沙塵測試')}")
check("數字相鄰不算（CE102 的 CE）", toks("CE102 的限制") == [], f"→ {toks('CE102 的限制')}")
check("大寫詞內部不算（OTHER 裡的 ER）", toks("OTHER WATER") == [],
      f"→ {toks('OTHER WATER')}")
check("空輸入安全", toks("") == [] and _SHORT_ABBREV_RE.findall("") == [])

# ── 合併順序 ────────────────────────────────────────────
# 名次才有意義（fuse 只看 enumerate 位置），分數不參與計算。
_bm25 = [(101, -4.5), (102, -4.4), (103, -4.3)]
_m = _merge_abbrev([7, 102], _bm25, top_k=10)
check("縮寫命中排在 BM25 名單前面", [f for f, _ in _m][:2] == [7, 102],
      f"→ {[f for f, _ in _m]}")
check("重複的 id 不出現兩次", [f for f, _ in _m].count(102) == 1)
check("BM25 其餘結果保留在後面", [f for f, _ in _m][2:] == [101, 103])
check("縮寫命中帶哨兵分數", _m[0][1] == _ABBREV_SCORE)
check("受 top_k 截斷", len(_merge_abbrev([7, 8, 9], _bm25, top_k=4)) == 4)
check("無縮寫命中時原樣返回", _merge_abbrev([], _bm25, top_k=10) == _bm25)
check("BM25 全空時只回縮寫", _merge_abbrev([7], [], top_k=10) == [(7, _ABBREV_SCORE)])

print()
print("全部通過 ✅" if all(results) else f"有 {results.count(False)} 項失敗 ❌")
sys.exit(0 if all(results) else 1)
