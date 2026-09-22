"""plans + organizations — the admin control plane's first schema.

Revision ID: 0001
Revises:
Create Date: 2026-09-07

Hand-aligned to the ORM models (modules.plans.models.Plan,
modules.organizations.models.Organization). Columns are NOT NULL with no server_default —
the ORM supplies Python-side defaults (uuid4, now(), 0, false), keeping behavior identical
on aiosqlite and Postgres (ADR-015a). The FK carries ON DELETE RESTRICT as the Postgres
backstop for the app-level referential rule (AD-06).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plans",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("tpm", sa.Integer(), nullable=False),
        sa.Column("rpm", sa.Integer(), nullable=False),
        sa.Column("quota_monthly_tokens", sa.BigInteger(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "organizations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
        sa.UniqueConstraint("slug"),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["plans.id"], name="fk_organizations_plan_id", ondelete="RESTRICT"
        ),
    )


def downgrade() -> None:
    op.drop_table("organizations")
    op.drop_table("plans")
