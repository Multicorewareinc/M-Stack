"""Pydantic v2 DTOs for RBAC resolution responses."""

from __future__ import annotations

import uuid

from pydantic import BaseModel


class PermissionIds(BaseModel):
    permission_ids: list[uuid.UUID]


class RoleWithPermissions(BaseModel):
    role_id: uuid.UUID
    name: str
    permission_ids: list[uuid.UUID]
