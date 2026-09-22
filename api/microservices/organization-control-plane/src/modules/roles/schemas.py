"""Pydantic v2 DTOs for roles and the composition/assignment set operations."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class RoleCreate(BaseModel):
    name: str = Field(min_length=1)
    description: str | None = None


class RoleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    description: str | None = None


class RoleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    description: str | None
    is_system_role: bool
    created_at: datetime
    updated_at: datetime


class PermissionSet(BaseModel):
    permission_ids: list[uuid.UUID]


class RoleSet(BaseModel):
    role_ids: list[uuid.UUID]


class PermissionUsage(BaseModel):
    """Whether a master-list permission id is composed into any role, in any org — the response
    shape for Admin CP's reverse-reference check before deleting a permission."""

    in_use: bool
