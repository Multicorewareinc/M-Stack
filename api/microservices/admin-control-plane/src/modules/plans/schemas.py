"""Pydantic v2 DTOs for plans. `is_default` is server-controlled — absent from the input
schemas, present only on output."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class PlanCreate(BaseModel):
    name: str = Field(min_length=1)
    tpm: int = Field(default=0, ge=0)
    rpm: int = Field(default=0, ge=0)
    quota_monthly_tokens: int = Field(default=0, ge=0)
    stripe_price_id: str | None = None


class PlanUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    tpm: int | None = Field(default=None, ge=0)
    rpm: int | None = Field(default=None, ge=0)
    quota_monthly_tokens: int | None = Field(default=None, ge=0)
    is_active: bool | None = None
    stripe_price_id: str | None = None


class PlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    tpm: int
    rpm: int
    quota_monthly_tokens: int
    is_default: bool
    is_active: bool
    stripe_price_id: str | None
    created_at: datetime
    updated_at: datetime
