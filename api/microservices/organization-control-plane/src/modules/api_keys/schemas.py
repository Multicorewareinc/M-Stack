"""Pydantic v2 DTOs for API keys.

`ApiKeyOut` carries metadata only and NEVER the raw secret; `status` is a computed field derived from
`revoked_at`/`expires_at`/now (revoked > expired > active — precedence order), never a stored column.
`ApiKeyCreated` is the ONLY shape that ever transmits the plaintext `raw_key`, returned once on create
and on rotate (ADR-028, reference §5)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field


def compute_status(revoked_at: datetime | None, expires_at: datetime | None) -> str:
    """Derived key status. Revoked outranks expired outranks active. Expiry is inclusive
    (`expires_at <= now` ⇒ expired)."""
    if revoked_at is not None:
        return "revoked"
    if expires_at is not None:
        # aiosqlite (offline tests) reads timestamps back naive; asyncpg returns tz-aware. Normalize
        # a naive value to UTC so the comparison is always aware-vs-aware (portability, ADR-015a).
        exp = expires_at if expires_at.tzinfo is not None else expires_at.replace(tzinfo=UTC)
        if exp <= datetime.now(UTC):
            return "expired"
    return "active"


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    expires_at: datetime | None = None


class ApiKeyUpdate(BaseModel):
    # `name` is non-clearable: an explicit null is rejected in the service (via model_fields_set).
    name: str | None = Field(default=None, min_length=1, max_length=128)
    # `expires_at` is clearable: an explicit null clears it (distinguished from "absent" in the
    # service via model_fields_set).
    expires_at: datetime | None = None


class ApiKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    org_id: uuid.UUID
    owner_id: uuid.UUID
    name: str
    prefix: str
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def status(self) -> Literal["active", "expired", "revoked"]:
        return compute_status(self.revoked_at, self.expires_at)  # type: ignore[return-value]


class ApiKeyCreated(ApiKeyOut):
    # The only shape that ever carries the plaintext value — returned once on create and rotate.
    raw_key: str


class ApiKeyVerifyOut(BaseModel):
    """Minimal record for the model-gateway verify path (SP-02, ADR-026). Carries the raw fields the
    gateway re-validates against `now` on every request — never the raw key or its hash."""

    id: uuid.UUID
    org_id: uuid.UUID
    owner_id: uuid.UUID
    revoked_at: datetime | None
    expires_at: datetime | None
    owner_active: bool
