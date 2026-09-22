"""Organization tenant ORM model — the org-level operational record (rbac doc §10). The `id` is
the GLOBAL organization id supplied by Admin CP (no server-side default), so the Admin PG and
Org PG records are related by identity, not two authorities (D1). Portable column types only so
this runs unchanged on aiosqlite and Postgres (ADR-015a); `settings` uses SQLAlchemy `JSON`."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from db import Base
from sqlalchemy import JSON, DateTime, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(UTC)


class Organization(Base):
    __tablename__ = "organizations"

    # No default: the id is supplied by Admin CP (the global organization id), not generated here.
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    settings: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )
