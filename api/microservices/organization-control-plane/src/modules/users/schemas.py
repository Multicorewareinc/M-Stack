"""Pydantic v2 DTOs for users. The output `metadata` key maps to the ORM `meta` attribute via a
serialization alias (FastAPI serializes response models by alias by default)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class UserCreate(BaseModel):
    username: str = Field(min_length=1)
    email: str = Field(min_length=1)
    first_name: str | None = None
    last_name: str | None = None
    display_name: str | None = None
    metadata: dict = Field(default_factory=dict)
    # Optional initial password for local auth (provisioning follow-up, ADR-025/AD-06: org users are
    # provisioned into an org first, not self-registered). Capped so an over-large payload can't be
    # fed into Argon2id (same DoS guard as the /api/auth schemas). Omitted/None -> password_hash stays
    # NULL (the user cannot log in locally until an admin sets one, unchanged prior behavior).
    password: str | None = Field(default=None, max_length=4096)


class UserUpdate(BaseModel):
    username: str | None = Field(default=None, min_length=1)
    email: str | None = Field(default=None, min_length=1)
    first_name: str | None = None
    last_name: str | None = None
    display_name: str | None = None
    status: Literal["active", "suspended"] | None = None
    metadata: dict | None = None
    # Admin-initiated password (re)set (air-gapped: no self-service email reset, ADR-008 §5). When
    # present, the service hashes it and forces rotation on next login (must_change_password=True) —
    # the same semantic as the seed_admin bootstrap.
    password: str | None = Field(default=None, max_length=4096)


class UserCounts(BaseModel):
    # Keys are stringified org UUIDs — one bulk call for Admin CP's organizations list, never a
    # per-org round trip (no N+1, §184).
    counts: dict[str, int]


class ActiveUserCount(BaseModel):
    count: int


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    username: str
    email: str
    first_name: str | None
    last_name: str | None
    display_name: str | None
    status: str
    # ORM attribute is `meta`; emit it as `metadata` in the response.
    meta: dict = Field(serialization_alias="metadata")
    created_at: datetime
    updated_at: datetime
    last_login_at: datetime | None
