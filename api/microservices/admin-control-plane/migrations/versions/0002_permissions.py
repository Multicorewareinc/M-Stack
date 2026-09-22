"""permissions — the master permission list (ADR-019).

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-09

Hand-aligned to the ORM model (modules.permissions.models.Permission). Columns are NOT NULL
(except nullable description) with no server_default — the ORM supplies Python-side defaults
(uuid4, now(), true), keeping behaviour identical on aiosqlite and Postgres (ADR-015a). The
global catalog carries NO organization_id; `slug` is UNIQUE.
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
        "permissions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("resource", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )


def downgrade() -> None:
    op.drop_table("permissions")
