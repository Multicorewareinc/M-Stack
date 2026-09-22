"""Auth ORM — the org-less super-admin credential store + refresh-token table (design D1/D2).

`AdminUser` is a fresh table (Admin CP has no existing users table): an org-less super-admin with a
local password hash. There is NO `role` column (this table only ever holds super-admins, so the
`role` claim is the constant `"super_admin"` in the encoder — see jwt.py) and NO `organization_id`.
`RefreshToken` is one issued refresh token, stored as a SHA-256 hash (never the raw token) with a
hard expiry and a nullable revocation timestamp; rotation + reuse-detection run against it in
service.py.

Portable column types only (ADR-015a): `Uuid` with a Python-side default (NOT server_default
gen_random_uuid), `String`/`String(64)`, `DateTime(timezone=True)`, so this runs unchanged on
aiosqlite (offline tests) and Postgres (prod). Every NOT NULL column a raw-SQL writer (SP-03's
seed_admin, the PG test) could omit carries a `server_default` (SP-01 Postgres lesson).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from db import Base
from sqlalchemy import Boolean, DateTime, ForeignKey, String, Uuid, false, true
from sqlalchemy.orm import Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(UTC)


class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Stored lowercased (the service normalises before insert); UNIQUE + indexed for the login lookup.
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    # NULL until a password is set (SP-03's seed_admin sets it); a NULL-hash account cannot log in.
    password_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    # server_default so a raw-SQL writer (seed_admin / the PG test) that omits it still inserts.
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    # Dedicated deactivation flag (Admin CP has no status vocabulary); server_default true likewise.
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # SHA-256 hex digest of the raw token (64 chars). Unique so a presented token resolves to at
    # most one row; the lookup path for refresh + reuse detection.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
