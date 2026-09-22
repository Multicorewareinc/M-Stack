"""plans.stripe_price_id — links a plan to a Stripe Price (ADR-031 AD-06 / billing-plan-
subscription-linkage).

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-17

Nullable, no server_default needed (a null price is a valid, inert state — a plan with no
Stripe price configured yet, e.g. a Free tier).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("plans", sa.Column("stripe_price_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("plans", "stripe_price_id")
