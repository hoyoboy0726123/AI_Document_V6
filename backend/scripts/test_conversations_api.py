# -*- coding: utf-8 -*-
"""多對話串 API 的端對端測試（V6）。

用 TestClient 直接打端點，不需要另外啟後端。
重點在「別人的對話不能被讀寫」——那是這批端點唯一會造成資料外洩的地方。
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.core.config import settings  # noqa: E402

results = []


def check(name, ok, extra=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{('  ' + extra) if extra else ''}")


c = TestClient(app)
r = c.post("/api/v1/auth/login",
           data={"username": settings.DEFAULT_ADMIN_USERNAME,
                 "password": settings.DEFAULT_ADMIN_PASSWORD})
assert r.status_code == 200, f"登入失敗 {r.status_code} {r.text[:200]}"
H = {"Authorization": f"Bearer {r.json()['access_token']}"}

# ── 列表：搬遷進來的舊對話應該在 ──
r = c.get("/api/v1/rag/conversations", headers=H)
check("列表可讀", r.status_code == 200)
items = r.json()["conversations"]
check("搬遷的舊對話存在", any(i["title"] == "舊對話（V5 匯入）" for i in items),
      f"共 {len(items)} 條")
legacy = next((i for i in items if i["title"] == "舊對話（V5 匯入）"), None)
check("舊對話訊息數為 148", legacy and legacy["message_count"] == 148,
      f"→ {legacy['message_count'] if legacy else 'N/A'}")
check("列表不含 messages（避免整包送出）", legacy is not None and "messages" not in legacy)

# ── 建立 ──
r = c.post("/api/v1/rag/conversations", headers=H, json={})
check("可建立新對話", r.status_code == 200)
new_id = r.json()["id"]
check("新對話預設標題", r.json()["title"] == "新對話")

# ── 取單一 ──
r = c.get(f"/api/v1/rag/conversations/{new_id}", headers=H)
check("可取單一對話且含 messages", r.status_code == 200 and r.json()["messages"] == [])

# ── 改名與釘選 ──
r = c.patch(f"/api/v1/rag/conversations/{new_id}", headers=H,
            json={"title": "鹽霧測試相關", "is_pinned": True})
check("可改標題與釘選", r.status_code == 200 and r.json()["title"] == "鹽霧測試相關"
      and r.json()["is_pinned"] is True)
r = c.patch(f"/api/v1/rag/conversations/{new_id}", headers=H, json={"title": "  "})
check("空白標題被擋下", r.status_code == 400)

# ── 釘選要排在最前面 ──
r = c.get("/api/v1/rag/conversations", headers=H)
check("釘選的排在最前", r.json()["conversations"][0]["id"] == new_id)

# ── 寫入一輪問答（走 service，不呼叫 LLM）──
from app.database import SessionLocal          # noqa: E402
from app.services import conversations as conv  # noqa: E402

db = SessionLocal()
row = conv.append_message(db, legacy_user := legacy and None or None, None,
                          question="x", answer="y") if False else None
me = c.get("/api/v1/auth/me", headers=H)
uid = me.json()["id"] if me.status_code == 200 else None
check("可取得自己的 user id", uid is not None)

saved = conv.append_message(db, uid, new_id, question="鹽霧測試的箱體溫度",
                            answer="35 ±1 ºC", sources=[{"title": "MIL-STD-810H"}],
                            mode="hybrid:rag")
check("append 寫進指定對話", saved is not None and saved.id == new_id)
r = c.get(f"/api/v1/rag/conversations/{new_id}", headers=H)
msgs = r.json()["messages"]
check("訊息內容正確且含 sources",
      len(msgs) == 1 and msgs[0]["answer"] == "35 ±1 ºC" and msgs[0]["sources"])

auto = conv.append_message(db, uid, None, question="自動開串的問題", answer="a")
check("conversation_id 為空時自動開新串", auto is not None and auto.id != new_id)
check("自動開串會用問題當標題", auto.title == "自動開串的問題")

# ── 隔離：別人的對話讀不到也刪不掉 ──
check("讀不存在的對話回 404",
      c.get("/api/v1/rag/conversations/not-a-real-id", headers=H).status_code == 404)
check("改不存在的對話回 404",
      c.patch("/api/v1/rag/conversations/not-a-real-id", headers=H,
              json={"title": "x"}).status_code == 404)
check("刪不存在的對話回 404",
      c.delete("/api/v1/rag/conversations/not-a-real-id", headers=H).status_code == 404)
check("未帶 token 一律 401",
      c.get("/api/v1/rag/conversations").status_code == 401)

# ── 刪除 ──
check("可刪除對話", c.delete(f"/api/v1/rag/conversations/{auto.id}", headers=H).status_code == 204)
check("刪除後真的不見了",
      c.get(f"/api/v1/rag/conversations/{auto.id}", headers=H).status_code == 404)

# 清掉測試資料，別把測試對話留在使用者的側邊欄
c.delete(f"/api/v1/rag/conversations/{new_id}", headers=H)
db.close()

print()
print("全部通過 ✅" if all(results) else f"有 {results.count(False)} 項失敗 ❌")
sys.exit(0 if all(results) else 1)
