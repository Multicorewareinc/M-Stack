"""local auth — admin_users credential store + refresh_tokens (add-admin-cp-local-auth).

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-11

Additive, forward-only (D1/D2): creates `admin_users` (the org-less super-admin store — NO role
column, NO organization_id) and `refresh_tokens` (SHA-256 hash stored, never the raw token; FK →
admin_users ON DELETE CASCADE). Portable types (ADR-015a): Uuid PK (ORM-side default), String(64)
unique token_hash, timezone-aware timestamps. Every NOT NULL column a raw-SQL writer (SP-03's
seed_admin CLI, the PG test) could omit carries a `server_default` — `must_change_password` (false)
and `active` (true) — the SP-01 Postgres lesson; the ORM still sets them explicitly.
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
        "admin_users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=True),
        sa.Column(
            "must_change_password", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email", name="uq_admin_users_email"),
    )
    op.create_index("ix_admin_users_email", "admin_users", ["email"])

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
            ["user_id"], ["admin_users.id"], name="fk_refresh_tokens_user_id", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
    )
    # token_hash is already indexed by uq_refresh_tokens_token_hash; only user_id needs its own.
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_refresh_tokens_user_id", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
    op.drop_index("ix_admin_users_email", table_name="admin_users")
    op.drop_table("admin_users")
