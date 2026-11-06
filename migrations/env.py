"""
File:   env.py
Brief:  Alembic migration environment for the TapeWebUI catalog.
Author: Mistress-Lukutar
Date:   2026-11-06
Version: v0.1.0
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from tapevaultui.config import get_settings
from tapevaultui.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _db_url() -> str:
    """Resolve the catalog database URL.

    Returns:
        SQLAlchemy URL: the alembic.ini value if set, then TAPE_DB_URL
        env override, then the sqlite catalog path from app settings.
    """
    ini_url = config.get_main_option("sqlalchemy.url")
    if ini_url:
        return ini_url
    override = os.environ.get("TAPE_DB_URL")
    if override:
        return override
    settings = get_settings()
    settings.ensure_dirs()
    return f"sqlite:///{settings.catalog_db_path}"


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a DBAPI)."""
    context.configure(
        url=_db_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode with a live connection."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _db_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
