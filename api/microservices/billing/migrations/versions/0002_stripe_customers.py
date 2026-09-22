"""stripe_customers — org-to-Stripe-Customer mapping (ADR-031 AD-06).

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-17

Hand-aligned to the ORM model (models.StripeCustomer). Columns are NOT NULL with no
server_default — the ORM supplies Python-side defaults (uuid4, now()), keeping behavior
identical on aiosqlite and Postgres (ADR-015a). `principal` carries the UNIQUE constraint that
makes Stripe Customer creation idempotent: at most one Stripe Customer per org.
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
        "stripe_customers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("principal", sa.String(), nullable=False),
        sa.Column("stripe_customer_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("principal", name="uq_stripe_customers_principal"),
    )


def downgrade() -> None:
    op.drop_table("stripe_customers")
