"""plans.is_active — soft-deactivate (mirrors Permission.is_active).

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-10

Adds a NOT NULL column to an existing table, so unlike 0001/0002 this DOES need a
server_default (existing rows must get a value at ALTER time) — true for every plan created
before this migration, matching the ORM's Python-side default for newly created rows.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plans",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column("plans", "is_active")
