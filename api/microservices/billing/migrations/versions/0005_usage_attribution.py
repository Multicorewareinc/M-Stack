"""usage gains per-request attribution — api_key_id, owner_id
(add-billing-usage-attribution).

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-17

Two nullable columns, no server_default (mirrors 0003's rationale): NULL is a true statement
for every existing row — a usage row metered before the attribution pipeline shipped genuinely
has no api_key_id/owner_id, not a placeholder. FK-less opaque strings (no cross-service FK,
database-per-service — same as the existing `principal` column). Pure ADD COLUMN, no backfill.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("usage", sa.Column("api_key_id", sa.String(), nullable=True))
    op.add_column("usage", sa.Column("owner_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("usage", "owner_id")
    op.drop_column("usage", "api_key_id")
