"""API key ORM model — user-owned, org-scoped bearer credentials (ADR-027/ADR-028). Owned by Org CP.
Only the SHA-256 `key_hash` is stored, never the raw value. Status (revoked > expired > active) is
derived at read time, never a stored column. Portable column types only (ADR-015a)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from db import Base
from sqlalchemy import DateTime, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(UTC)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Tenant scope — every query filters on this. Sourced from the authenticated principal only.
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    # The owning user (user-owned keys only this increment; no owner_kind — ADR-027).
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # Deterministic hex SHA-256 of the raw key — the only persisted form. Unique so a hash resolves
    # to exactly one key (the model-gateway verify lookup, SP-02).
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # Display/log-safe fragment (never the full secret).
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    # NULL = durable/never-expiring.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Non-null = revoked (soft-delete).
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
