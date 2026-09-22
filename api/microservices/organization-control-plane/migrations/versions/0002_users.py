"""users — the Org CP authoritative user record (tenant-scoped).

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-09

Hand-aligned to modules.users.models.User. Tenant-scoped by organization_id (FK →
organizations.id ON DELETE RESTRICT). Email and username are unique WITHIN an organization
(composite UNIQUE), so different orgs may reuse them. `metadata` column, `last_login_at`
nullable. Portable types; ORM supplies Python-side defaults (ADR-015a).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("first_name", sa.String(), nullable=True),
        sa.Column("last_name", sa.String(), nullable=True),
        sa.Column("display_name", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], name="fk_users_org_id", ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("organization_id", "email", name="uq_users_org_email"),
        sa.UniqueConstraint("organization_id", "username", name="uq_users_org_username"),
    )
    op.create_index("ix_users_organization_id", "users", ["organization_id"])


def downgrade() -> None:
    op.drop_index("ix_users_organization_id", table_name="users")
    op.drop_table("users")
