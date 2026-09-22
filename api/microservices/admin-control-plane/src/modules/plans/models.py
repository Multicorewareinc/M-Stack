"""Plan ORM model. Portable column types only (Uuid / BigInteger / DateTime(timezone=True)
with Python-side defaults) so this runs unchanged on both aiosqlite and Postgres — ADR-015a."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from db import Base
from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(UTC)


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    # 0 = unlimited (matches the rate-limiter convention).
    tpm: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rpm: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quota_monthly_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # Server-controlled: only the seed sets this true; never accepted from client input.
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Soft-deactivate only (mirrors Permission.is_active) — a deactivated plan stays attached to
    # any organization already on it; new organizations should not be assigned to it.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Nullable: no Stripe price configured yet (e.g. a Free tier) is a valid, inert state —
    # billing treats a null price as a documented no-op, never an error (ADR-031 AD-06).
    # admin-control-plane never validates this against Stripe; billing is the sole Stripe caller.
    stripe_price_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )
