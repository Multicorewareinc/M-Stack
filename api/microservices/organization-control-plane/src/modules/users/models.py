"""User ORM model — the authoritative user record (rbac doc §16), owned by Org CP. Every row is
tenant-scoped by `organization_id`; email and username are unique WITHIN an organization
(different orgs may reuse them). Portable column types only (ADR-015a).

Note: the ORM attribute is `meta`, mapped to the DB column `metadata` — SQLAlchemy reserves
`Model.metadata` for the table's MetaData, so the attribute cannot be named `metadata` (D5)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from db import Base
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    false,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(UTC)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("organization_id", "email", name="uq_users_org_email"),
        UniqueConstraint("organization_id", "username", name="uq_users_org_username"),
        # Local-auth login (modules/auth/service.py) looks up `lower(email)` GLOBALLY — it has no
        # org context to scope by, since a session is issued before any org is known. Email is only
        # unique PER ORG above, so two orgs could otherwise each have a user sharing an email who
        # both have a password set, making that lookup return an arbitrary one of them (SQLAlchemy's
        # plain `.scalar()` does not raise on multiple rows — silent wrong-account auth, not just an
        # error). This partial unique index makes that state impossible to create: email must be
        # globally unique ONLY among rows that can actually log in locally (password_hash set).
        # Org-directory rows with no password may still safely reuse an email across orgs, matching
        # existing behavior. Portable (ADR-015a): partial index on both Postgres and SQLite (offline
        # tests) via the two dialect-specific `_where` kwargs.
        Index(
            "uq_users_local_auth_email",
            text("lower(email)"),
            unique=True,
            postgresql_where=text("password_hash IS NOT NULL"),
            sqlite_where=text("password_hash IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    username: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str] = mapped_column(String, nullable=False)
    first_name: Mapped[str | None] = mapped_column(String, nullable=True)
    last_name: Mapped[str | None] = mapped_column(String, nullable=True)
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    # DB column is `metadata`; attribute is `meta` (SQLAlchemy reserves Model.metadata).
    meta: Mapped[dict] = mapped_column("metadata", JSON, nullable=False, default=dict)
    # Local-auth credential columns (add-org-cp-local-auth, D1). password_hash is nullable — a row
    # with no hash cannot log in (existing rows, OIDC-only). must_change_password gates the
    # forced-rotation flow. Portable types (ADR-015a): String / Boolean, ORM-side default.
    password_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    # server_default matches migration 0004 so raw-SQL / existing writers that omit the column insert
    # safely (parity with the Alembic DDL); the ORM also sets the Python-side default.
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def active(self) -> bool:
        # ponytail: `active` is derived from status == 'active' (ceiling: the users directory has a
        # single status vocabulary and 'active' is the only usable state). No new `active` column;
        # upgrade path = an additive Boolean column if a status-independent activation flag is ever
        # needed. The login deactivation gate (service.login_local) reads this.
        return self.status == "active"
