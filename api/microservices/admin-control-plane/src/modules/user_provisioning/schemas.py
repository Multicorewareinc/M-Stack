"""Request DTOs for super-admin user provisioning (ADR-029/AD-02b,AD-02c). A password is required
so the created user is login-capable immediately (Org CP hashes it + forces rotation)."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class OwnerCreate(BaseModel):
    username: str = Field(min_length=1)
    email: str = Field(min_length=1)
    # Required here (unlike the Org CP UserCreate where it is optional): a super-admin-provisioned
    # user/owner must be able to log in right away. Capped so an over-large value can't be fed into
    # Argon2id downstream (same guard as the /api/auth schemas).
    password: str = Field(min_length=1, max_length=4096)
    first_name: str | None = None
    last_name: str | None = None
    display_name: str | None = None


class OrgWithOwnerCreate(BaseModel):
    name: str = Field(min_length=1)
    plan_id: uuid.UUID
    owner: OwnerCreate
