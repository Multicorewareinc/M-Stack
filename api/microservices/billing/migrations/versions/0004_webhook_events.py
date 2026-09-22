"""webhook_events — idempotency record for processed Stripe webhook events
(add-billing-stripe-webhooks).

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-17

Hand-aligned to the ORM model (models.WebhookEvent). Columns are NOT NULL with no
server_default — the ORM supplies Python-side defaults (uuid4, now()), keeping behavior
identical on aiosqlite and Postgres (ADR-015a). `stripe_event_id` carries the UNIQUE
constraint that is this endpoint's idempotency guarantee.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "webhook_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("stripe_event_id", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stripe_event_id", name="uq_webhook_events_stripe_event_id"),
    )


def downgrade() -> None:
    op.drop_table("webhook_events")
