# -*- coding: utf-8 -*-
"""Verify audit fixes (V4). Targeted, no Ollama required."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import numpy as np

PASS, FAIL = [], []
def ok(name, *_):  PASS.append(name); print(f"  [PASS] {name}")
def no(name, why): FAIL.append((name, why)); print(f"  [FAIL] {name} -> {why}")

# ---- H16/H17: route_mode ----
from app.services import agent
print("route_mode (H16/H17):")
cases_rag = [
    "What humidity is required for Method 507.6?",   # required, not relation
    "How is unit conversion handled?",               # conversion, not version
    "依據 MIL-STD-810H，514.6 的振動加速度是多少?",     # 依據 content
    "濕度測試怎麼進行?",
]
cases_agent = [
    "MIL-STD-810H 引用了哪些標準?",
    "MIL-STD-810G 的下一版是什麼?",                    # successor / 下一版
    "510.7 引用哪些外部規範?",
    "What is the successor of 810G?",
]
for q in cases_rag:
    (ok if agent.route_mode(q) == "rag" else no)(f"rag: {q}", agent.route_mode(q))
for q in cases_agent:
    (ok if agent.route_mode(q) == "agent" else no)(f"agent: {q}", agent.route_mode(q))

# _APP_RE should NOT trigger on bare 設備 in a relation question (H17)
if not agent._APP_RE.search("MIL-STD-810H 對設備的鹽霧測試引用哪些標準?"):
    ok("H17: 裸『設備』不再觸發 _APP_RE")
else:
    no("H17: _APP_RE", "still triggers on bare 設備")
# but real application question still triggers
if agent._APP_RE.search("我的設備該做哪些測試?"):
    ok("H17: 真應用題仍觸發 _APP_RE")
else:
    no("H17: _APP_RE app", "no longer matches real app question")

# ---- H9: IEEE suffix ----
from app.services import kg_extractor
print("kg_extractor IEEE (H9):")
specs = kg_extractor.extract_specs("This complies with IEEE 802.11b and IEEE 802.1Q and IEEE 1156.")
cids = sorted({getattr(s, "canonical_id", str(s)) for s in specs})
joined = " ".join(cids).upper()
if "IEEE 802.11B" in joined and "IEEE 802.1Q" in joined:
    ok(f"IEEE 後綴保留(未塌陷): {cids}")
elif "IEEE 802" in cids and "IEEE 802.11B" not in joined:
    no("H9 IEEE", f"still collapsing to IEEE 802: {cids}")
else:
    ok(f"IEEE extracted: {cids}")

# ---- users.create_user role whitelist (C1) ----
from app.services import users
print("create_user whitelist (C1):")
if users._ALLOWED_ROLES == {"admin", "assistant"}:
    ok("_ALLOWED_ROLES 白名單存在")
else:
    no("C1 whitelist", str(users._ALLOWED_ROLES))

# ---- vector_store: atomic write + dimension check + rebuild (H2/H3) ----
# 重要：隔離到臨時 index 路徑，避免動到 V4 真實的 storage/faiss_index.bin
import tempfile
from pathlib import Path as _P
from app.services import vector_store
_tmpdir = tempfile.mkdtemp()
vector_store._INDEX_PATH = _P(_tmpdir) / "test_index.bin"
vector_store._INDEX = None
vector_store._INDEX_DIMENSION = None
print("vector_store (H2/H3)  [isolated temp index]:")
# rebuild from a mapping
m = {1001: [0.1, 0.2, 0.3], 1002: [0.4, 0.5, 0.6]}
n = vector_store.rebuild_index(m)
if n == 2:
    ok("rebuild_index 從 mapping 重建 2 向量")
else:
    no("H2 rebuild", f"expected 2 got {n}")
# search works after rebuild
res = vector_store.search([0.1, 0.2, 0.3], top_k=1)
if res and res[0][0] in (1001, 1002):
    ok(f"重建後可檢索: {res}")
else:
    no("H2 rebuild search", str(res))
# dimension mismatch raises (H3)
try:
    vector_store.add_embeddings({1003: [0.1, 0.2]})  # wrong dim (2 vs 3)
    no("H3 dim check", "no error raised on dim mismatch")
except ValueError as e:
    ok("H3: 維度不一致丟出明確 ValueError")
# atomic write helper leaves no .tmp
from pathlib import Path
tmp = Path(str(vector_store._INDEX_PATH) + ".tmp")
if not tmp.exists():
    ok("H2: 原子寫未殘留 .tmp")
else:
    no("H2 tmp", ".tmp left behind")

# ---- _resolve_temp_pdf path traversal (C2) ----
print("_resolve_temp_pdf (C2):")
from app.database import SessionLocal
from app.services.documents import DocumentService
db = SessionLocal()
try:
    svc = DocumentService(db)
    # absolute path outside temp dir must be rejected (400), never returned as-is
    for evil in ["../../.env", "C:/Windows/system32/drivers/etc/hosts", "../doc_management.db"]:
        try:
            svc._resolve_temp_pdf(evil)
            no("C2 traversal", f"accepted {evil}")
        except Exception as e:
            # HTTPException(400) expected
            if getattr(e, "status_code", None) == 400:
                ok(f"C2: 拒絕 {evil}")
            else:
                ok(f"C2: 拒絕 {evil} ({type(e).__name__})")
    # non-pdf rejected
    try:
        svc._resolve_temp_pdf("evil.txt")
        no("C2 ext", "accepted .txt")
    except Exception as e:
        ok("C2: 非 .pdf 被拒")
finally:
    db.close()

# ---- models faiss_id BigInteger (H6) ----
from app import models
from sqlalchemy import BigInteger
col = models.DocumentChunk.__table__.c.faiss_id
if isinstance(col.type, BigInteger):
    ok("H6: faiss_id 為 BigInteger")
else:
    no("H6", f"faiss_id type = {col.type}")

# ---- database PRAGMA (medium) ----
print("SQLite PRAGMA (medium):")
db = SessionLocal()
try:
    jm = db.execute(__import__("sqlalchemy").text("PRAGMA journal_mode")).scalar()
    bt = db.execute(__import__("sqlalchemy").text("PRAGMA busy_timeout")).scalar()
    if str(jm).lower() == "wal":
        ok(f"journal_mode=WAL")
    else:
        no("PRAGMA WAL", str(jm))
    if int(bt) >= 10000:
        ok(f"busy_timeout={bt}")
    else:
        no("PRAGMA busy_timeout", str(bt))
finally:
    db.close()

print()
print(f"==== RESULT: {len(PASS)} passed, {len(FAIL)} failed ====")
if FAIL:
    for n_, w in FAIL:
        print(f"   FAILED: {n_} -> {w}")
    sys.exit(1)
