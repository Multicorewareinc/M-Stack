"""usage + outbox — billing's first schema (ADR-031).

Revision ID: 0001
Revises:
Create Date: 2026-09-16

Hand-aligned to the ORM models (models.Usage, models.Outbox). Columns are NOT NULL with no
server_default — the ORM supplies Python-side defaults (uuid4, now(), "pending", 0), keeping
behavior identical on aiosqlite and Postgres (ADR-015a). `usage.request_id` carries the
UNIQUE constraint that is this service's idempotency guarantee (ADR-031 AD-04).
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
        "usage",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("principal", sa.String(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id", name="uq_usage_request_id"),
    )
    op.create_table(
        "outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("outbox")
    op.drop_table("usage")
