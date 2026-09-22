"""RefreshToken ORM — the durable store backing platform session continuity (design D2).

Each row is one issued refresh token, stored as a SHA-256 hash (never the raw token), with a hard
expiry and a nullable revocation timestamp. Rotation-on-use and revoked-but-reused detection (the
stolen-cookie defence) are implemented against this table by modules/auth/service.py.

Portable column types only (ADR-015a): `Uuid` with a Python-side default (NOT server_default
gen_random_uuid), `String(64)` for the hex digest, `DateTime(timezone=True)`. Mirrors the users
model's type choices so this runs unchanged on aiosqlite (offline tests) and Postgres (prod).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from db import Base
from sqlalchemy import DateTime, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(UTC)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # SHA-256 hex digest of the raw token (64 chars). Unique so a presented token resolves to at
    # most one row; the lookup path for refresh + reuse detection.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
