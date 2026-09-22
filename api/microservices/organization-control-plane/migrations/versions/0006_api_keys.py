"""api_keys — user-owned, org-scoped bearer credentials (add-org-api-key-lifecycle, SP-01).

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-15

Additive, forward-only. Only the SHA-256 `key_hash` is stored (unique). `org_id` is indexed (every
query filters on it). Portable types (ADR-015a): Uuid PK (ORM-side default), String(64) unique
`key_hash`, timezone-aware timestamps. No owner_kind / scopes / rate_limit columns (ADR-027/ADR-028).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_hash", name="uq_api_keys_key_hash"),
    )
    op.create_index("ix_api_keys_org_id", "api_keys", ["org_id"])


def downgrade() -> None:
    op.drop_index("ix_api_keys_org_id", table_name="api_keys")
    op.drop_table("api_keys")
