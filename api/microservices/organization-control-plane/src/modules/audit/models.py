"""Audit log ORM model — an append-only record of who did what, owned by Org CP. Org-scoped events
carry `organization_id` (the tenant filter for the Org Portal); the row is never updated or deleted
by the application. Portable column types only (ADR-015a)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from db import Base
from sqlalchemy import JSON, DateTime, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(UTC)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # NULL only for events with no resolvable tenant (e.g. a failed login for an unknown email).
    organization_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    # Who acted. actor_type: "user" (org member JWT), "admin" (platform super-admin acting through
    # Admin CP), "service" (an unattributed service-key caller), "anonymous" (unauthenticated, e.g. a
    # failed login). actor_email is denormalised so the log stays readable after a user is deleted.
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False, default="service")
    actor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor_email: Mapped[str | None] = mapped_column(String, nullable=True)
    # Dotted event name, e.g. "api_key.created".
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Human-readable subject (key name, user email, role name) — display only.
    resource_name: Mapped[str | None] = mapped_column(String, nullable=True)
    # Small structured context. NEVER holds secrets (passwords, raw keys).
    details: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, index=True
    )
