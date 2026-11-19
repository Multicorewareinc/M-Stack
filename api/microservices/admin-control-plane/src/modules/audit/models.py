"""Audit log ORM model — append-only record of platform-level (Admin CP) actions: organization
lifecycle, plans, the permission catalog and super-admin auth. Org-scoped events (users, roles, API
keys) live in Org CP's own audit table; the Admin Portal's list route merges both. Portable column
types only (ADR-015a)."""

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
    # The organization the event concerns, when there is one (org.created, org.plan_changed, ...).
    organization_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    # "admin" (a super-admin's JWT), "service" (the static admin key), "anonymous" (failed login).
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False, default="admin")
    actor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor_email: Mapped[str | None] = mapped_column(String, nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_name: Mapped[str | None] = mapped_column(String, nullable=True)
    # Small structured context. NEVER holds secrets.
    details: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, index=True
    )
