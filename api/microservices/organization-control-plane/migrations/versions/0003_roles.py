"""roles + role_permissions + user_roles — org-owned RBAC composition.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-09

Hand-aligned to modules.roles.models. `roles` is tenant-scoped with UNIQUE(organization_id,
name). `role_permissions.permission_id` references the Admin CP master list — NO FK (cross-
database, ADR-019). Link FKs on role_id/user_id CASCADE. Portable types (ADR-015a).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "roles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("is_system_role", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], name="fk_roles_org_id", ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("organization_id", "name", name="uq_roles_org_name"),
    )
    op.create_index("ix_roles_organization_id", "roles", ["organization_id"])
    op.create_table(
        "role_permissions",
        sa.Column("role_id", sa.Uuid(), nullable=False),
        # permission_id references the Admin CP master list — intentionally NO FK (ADR-019).
        sa.Column("permission_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("role_id", "permission_id"),
        sa.ForeignKeyConstraint(
            ["role_id"], ["roles.id"], name="fk_role_permissions_role_id", ondelete="CASCADE"
        ),
    )
    op.create_table(
        "user_roles",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "role_id"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_user_roles_user_id", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["role_id"], ["roles.id"], name="fk_user_roles_role_id", ondelete="CASCADE"
        ),
    )


def downgrade() -> None:
    op.drop_table("user_roles")
    op.drop_table("role_permissions")
    op.drop_index("ix_roles_organization_id", table_name="roles")
    op.drop_table("roles")
