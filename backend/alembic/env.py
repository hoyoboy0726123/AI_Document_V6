"""Alembic environment — wired to the app's settings and models."""
import os
import sys

from alembic import context
from sqlalchemy import engine_from_config, pool

# 讓 `app` 可被 import（alembic 從 backend/ 執行）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings  # noqa: E402
from app.database import Base  # noqa: E402
from app import models  # noqa: F401,E402  (import 觸發 model 註冊)

config = context.config
# 注意：不呼叫 fileConfig() —— 它會關閉/取代 app 既有的 logging handlers
# （包含包裝 stdout 的 handler），在「程式化執行 migration」時把 stdout 弄壞。
# alembic CLI 的日誌美化非必要，app 端已有自己的 logging 設定。

# 覆寫 URL 為 app 實際設定
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=url.startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite 不支援多數 ALTER —— batch mode 讓未來的欄位變更可行
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
