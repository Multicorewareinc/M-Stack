"""stripe_customers gains subscription linkage — stripe_subscription_id, stripe_price_id,
subscription_status (ADR-031 AD-06 part 2 / add-billing-plan-subscription-linkage).

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-17

All nullable, no server_default needed (NULL means "no subscription yet" for every existing
row, which is a true statement — Phase 1 never created a subscription).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("stripe_customers", sa.Column("stripe_subscription_id", sa.String(), nullable=True))
    op.add_column("stripe_customers", sa.Column("stripe_price_id", sa.String(), nullable=True))
    op.add_column("stripe_customers", sa.Column("subscription_status", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("stripe_customers", "subscription_status")
    op.drop_column("stripe_customers", "stripe_price_id")
    op.drop_column("stripe_customers", "stripe_subscription_id")
