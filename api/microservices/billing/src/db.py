"""Shared persistence primitives: the one DeclarativeBase, and the async engine/sessionmaker
builders (owns-it idiom lives in main.py). Mirrors admin-control-plane/src/db.py exactly.

No `get_session` FastAPI dependency: every write path here — the JetStream consumer, the
Stripe reporter, and the internal subscription-linkage endpoint (add-billing-plan-subscription-
linkage) — owns its own session lifecycle directly via `sessionmaker()`, rather than a
Request-scoped `Depends`. The subscription endpoint specifically needs to skip opening a
session entirely on its config-level inert path (no Stripe client configured) — a declared
FastAPI dependency is always resolved before the route body runs, so it cannot be conditionally
skipped; a plain `async with sessionmaker() as session:` inside the route body can be.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Single metadata target for this service's models and for Alembic."""


def build_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url)


def build_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
