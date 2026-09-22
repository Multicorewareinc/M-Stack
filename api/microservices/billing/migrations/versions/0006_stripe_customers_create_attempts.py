"""stripe_customers gains subscription_create_attempts.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-17

Counts `subscriptions.create` attempts per row, folded into that call's idempotency key so
each attempt gets a distinct one. server_default="0" so every existing row (all created before
any subscription attempt existed) starts at zero, the correct value for "never attempted yet".
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "stripe_customers",
        sa.Column("subscription_create_attempts", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("stripe_customers", "subscription_create_attempts")
