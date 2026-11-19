"""Invitation ORM model — a one-time, expiring offer for someone to join an organization in a given
role. Owned by Org CP, next to the users it creates. Only the SHA-256 `token_hash` is stored, never
the raw token (the link carries the raw value, shown once to whoever issued it). Status is
`pending` -> `accepted`, or `revoked` when a newer invitation replaces it; expiry is derived from
`expires_at` at read time. Portable column types only (ADR-015a)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from db import Base
from sqlalchemy import DateTime, ForeignKey, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(UTC)


class Invitation(Base):
    __tablename__ = "invitations"
    __table_args__ = (Index("ix_invitations_org_email", "organization_id", "email"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    role_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("roles.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    invited_by: Mapped[str | None] = mapped_column(String, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
