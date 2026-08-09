# -*- coding: utf-8 -*-
"""清掉既有 chunk 裡的 prompt 範例回聲，並用 PDF 原生文字層校正形近字。

不需要重跑 VL、不需要重新嵌入嗎？——需要重新嵌入。文字改了，向量就不對了，
所以有改動的 chunk 會被標記為需重新嵌入（清空 embedding 由既有的補嵌流程處理）。
只影響真的被改到的 chunk，沒改到的完全不動。

用法：.venv\\Scripts\\python.exe scripts\\clean_prompt_echo.py [--dry-run]
"""
from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sqlalchemy import text as sa_text  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.services.ai import _strip_prompt_echo, repair_code_tokens  # noqa: E402

DRY = "--dry-run" in sys.argv


def _native_by_page(pdf_path: str) -> dict:
    try:
        import fitz
        with fitz.open(pdf_path) as doc:
            return {i: pg.get_text() for i, pg in enumerate(doc, 1)}
    except Exception as exc:  # noqa: BLE001
        print(f"    （取原生文字層失敗，略過形近字校正：{exc}）")
        return {}


def main() -> int:
    db = SessionLocal()
    try:
        docs = db.execute(sa_text(
            "SELECT id, title, pdf_path FROM documents WHERE ocr_method = 'vision'"
        )).fetchall()
        print(f"走 VL 抽取的文件：{len(docs)} 份")
        total_echo = total_code = 0
        for did, title, path in docs:
            native = _native_by_page(path) if path else {}
            whole_native = "\n".join(native.values())
            rows = db.execute(sa_text(
                "SELECT id, page, text FROM document_chunks WHERE document_id = :d"
            ), {"d": did}).fetchall()
            changed = 0
            for cid, page, body in rows:
                new = _strip_prompt_echo(body)
                echo_hit = new != body
                # 回填時用「整份文件」的原生文字，不是單頁。chunk.page 是切塊的
                # 局部座標而非真實 PDF 頁碼，按頁對位會漏掉 —— 實測 22SWO 在兩個
                # chunk 出現，只有一個的 page 剛好對得上 11，另一個就漏修了。
                # 入庫流程沒有這個問題（VL 是逐頁跑的，頁碼精確），所以那裡仍用單頁。
                new, n_fix = repair_code_tokens(new, whole_native)
                if new == body:
                    continue
                changed += 1
                total_echo += int(echo_hit)
                total_code += n_fix
                if not DRY:
                    # embedding 一併清空：文字變了，舊向量就是錯的
                    db.execute(sa_text(
                        "UPDATE document_chunks SET text = :t, embedding = '[]' WHERE id = :i"
                    ), {"t": new, "i": cid})
            if changed:
                print(f"  {(title or '')[:40]:42s} 修改 {changed} 塊")
        if DRY:
            db.rollback()
        else:
            db.commit()
        print(f"\n範例回聲清除 {total_echo} 塊；形近字校正 {total_code} 處"
              f"{'（dry-run，已回滾）' if DRY else ''}")
        if not DRY and (total_echo or total_code):
            print("提醒：被修改的 chunk 已清空 embedding，需要跑一次補嵌入才會回到可檢索狀態。")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
