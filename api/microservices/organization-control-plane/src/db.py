"""Shared persistence primitives: the one DeclarativeBase the modules register on, the async
engine/sessionmaker builders (owns-it idiom lives in main.py), and the per-request session
dependency that owns the transaction. Copied verbatim from admin-control-plane (ADR-020).

Import direction: this is a leaf. Module `models.py` files import `Base` from here; nothing here
imports the modules (that would cycle).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Single metadata target for the modules' models and for Alembic."""


def build_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url)


def build_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield one AsyncSession per request and own its transaction: commit on normal return,
    roll back on any raised exception (typed AppError included), then close. Services flush()
    but never commit() — the single commit is here (design D3)."""
    sessionmaker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    session = sessionmaker()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
