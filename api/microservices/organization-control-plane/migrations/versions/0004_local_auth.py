"""local auth — users credential columns + refresh_tokens (add-org-cp-local-auth).

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-11

Additive, forward-only, no backfill (D2): adds `users.password_hash` (nullable) and
`users.must_change_password` (NOT NULL DEFAULT false — existing rows get false), and creates
`refresh_tokens` (user-owned, SHA-256 hash stored, never the raw token). Portable types (ADR-015a):
Uuid PK (ORM-side default), String(64) unique token_hash, timezone-aware timestamps. Existing rows
get a NULL hash → cannot log in until a password is set, which is correct.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("password_hash", sa.String(), nullable=True))
    op.add_column(
        "users",
        sa.Column(
            "must_change_password",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # Keep the server_default: it backfills existing rows AND lets any existing writer that omits
    # the column (raw-SQL callers, the pre-existing users PG test) insert safely. Dropping it would
    # make this a breaking change to `users` inserts. The ORM still sets the value explicitly.

    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_refresh_tokens_user_id", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
    )
    # token_hash is already indexed by uq_refresh_tokens_token_hash; only user_id needs its own.
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_refresh_tokens_user_id", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
    op.drop_column("users", "must_change_password")
    op.drop_column("users", "password_hash")
