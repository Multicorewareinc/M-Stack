"""Pydantic v2 DTOs for the master permission list. `slug` is server-derived
(`{resource}.{action}`) — absent from create input; `is_active` defaults true server-side."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


def slug_for(resource: str, action: str) -> str:
    return f"{resource}.{action}"


class PermissionCreate(BaseModel):
    resource: str = Field(min_length=1)
    action: str = Field(min_length=1)
    description: str | None = None


class PermissionUpdate(BaseModel):
    resource: str | None = Field(default=None, min_length=1)
    action: str | None = Field(default=None, min_length=1)
    description: str | None = None
    is_active: bool | None = None


class PermissionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    resource: str
    action: str
    slug: str
    description: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime
