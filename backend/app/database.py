
from sqlalchemy import create_engine, event
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from .core.config import settings

SQLALCHEMY_DATABASE_URL = settings.DATABASE_URL

_IS_SQLITE = SQLALCHEMY_DATABASE_URL.startswith("sqlite")

# SQLite 需要關閉 thread check
connect_args = {"check_same_thread": False} if _IS_SQLITE else {}

engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args=connect_args)


if _IS_SQLITE:
    # Audit medium：SQLite 併發加固。
    # - WAL：讀寫可並行，大幅降低多執行緒（finalize 任務 + kg worker + 請求）互鎖。
    # - busy_timeout=10s：撞鎖時等待而非立即 "database is locked"。
    # 註：foreign_keys 刻意不在此開啟 —— 既有 DB 可能存在歷史孤兒列，且需要完整
    #    ondelete 串接才安全（delete() 已在應用層清乾淨子表），留待 Alembic 遷移統一導入。
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=10000")
            cur.execute("PRAGMA synchronous=NORMAL")
        finally:
            cur.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


# Dependency to get DB session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
