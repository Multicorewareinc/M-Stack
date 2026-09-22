"""organizations — the Org CP tenant record (first Org PG schema).

Revision ID: 0001
Revises:
Create Date: 2026-09-09

Hand-aligned to the ORM model (modules.organizations.models.Organization). `id` is the global
organization id supplied by Admin CP (no server_default). `settings` is JSON. Columns are NOT
NULL with no server_default — the ORM supplies Python-side defaults (now(), 'active', {}),
keeping behaviour identical on aiosqlite and Postgres (ADR-015a).
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
        "organizations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("organizations")
