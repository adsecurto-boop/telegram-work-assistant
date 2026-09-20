import os
from logging.config import fileConfig
from sqlalchemy import pool
from alembic import context
from support_copilot.models import Base
from support_copilot.database import create_db_engine

config = getattr(context, "config", None)

if config is not None and config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

def get_database_url(cfg=None) -> str:
    current_cfg = cfg or getattr(context, "config", None)
    url = current_cfg.get_main_option("sqlalchemy.url") if current_cfg else None
    if not url:
        from support_copilot.config import get_settings
        settings = get_settings()
        url = settings.db_path
        if current_cfg:
            current_cfg.set_main_option("sqlalchemy.url", url)
    return url

def run_migrations_offline() -> None:
    url = get_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online() -> None:
    connectable = config.attributes.get("connection", None) if config else None
    created_engine = False
    if connectable is None:
        url = get_database_url()
        connectable = create_db_engine(url)
        created_engine = True

    try:
        with connectable.connect() as connection:
            context.configure(
                connection=connection, target_metadata=target_metadata
            )

            with context.begin_transaction():
                context.run_migrations()
    finally:
        if created_engine:
            connectable.dispose()

try:
    if context.is_offline_mode():
        run_migrations_offline()
    else:
        run_migrations_online()
except (NameError, AttributeError):
    pass
