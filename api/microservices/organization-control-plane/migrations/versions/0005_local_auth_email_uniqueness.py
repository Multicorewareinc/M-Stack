"""local auth — cross-org email uniqueness for local-auth-capable users.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-15

Local-auth login (modules/auth/service.py) looks up a user by `lower(email)` GLOBALLY — it runs
before any org is known, so it cannot scope by organization_id. Email is only unique PER ORG
(migration 0002), so two different orgs could each have a user sharing an email who both have a
password set; the login lookup would then silently resolve to an arbitrary one of them instead of
erroring (SQLAlchemy's `.scalar()` does not raise on multiple matching rows). This migration adds a
partial unique index — email unique across the whole table, but ONLY among rows that can actually
log in locally (`password_hash IS NOT NULL`) — so that ambiguous state can no longer be created.
Org-directory rows with no password may still safely share an email across orgs, matching existing
behavior (no data migration needed: this only constrains future inserts/updates that set a
password_hash colliding with an existing local-auth-capable row elsewhere).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_users_local_auth_email",
        "users",
        [sa.text("lower(email)")],
        unique=True,
        postgresql_where=sa.text("password_hash IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_users_local_auth_email", table_name="users")
