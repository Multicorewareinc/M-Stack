"""Pydantic v2 DTOs for organizations, plus the stdlib slug helper (no slugify dependency).
`status` is validated against the allowed set via a Literal."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

OrgStatus = Literal["active", "suspended"]


def slugify(name: str) -> str:
    """lowercase, non-alphanumeric -> '-', collapse repeats, strip ends. Stdlib only."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug


class OrgCreate(BaseModel):
    name: str = Field(min_length=1)
    plan_id: uuid.UUID


class OrgUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    plan_id: uuid.UUID | None = None
    status: OrgStatus | None = None


class OrgOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    plan_id: uuid.UUID
    status: str
    created_at: datetime
    updated_at: datetime
    # Backend-derived (never client-side per-row fan-out, §184): current Org CP user count and
    # whether a `failed` org can be retried. Attached by the service layer, not a DB column —
    # OrgOut is built via model_copy(update=...), not model_validate alone (see service.py).
    user_count: int = 0
    retryable: bool = False
