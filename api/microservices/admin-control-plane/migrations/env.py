"""Async Alembic environment — the repo's first. Reads DATABASE_URL from the environment and
targets the shared Base.metadata. Both model modules are imported so every table is
registered before autogenerate/upgrade runs."""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from db import Base
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

import modules.auth.models
import modules.organizations.models
import modules.permissions.models
import modules.plans.models  # noqa: F401 — register the plans table

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _get_url() -> str:
    return os.environ.get("DATABASE_URL", config.get_main_option("sqlalchemy.url") or "")


def run_migrations_offline() -> None:
    context.configure(
        url=_get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = create_async_engine(_get_url(), poolclass=pool.NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
