"""Pydantic v2 DTOs for the org tenant record. `id` is supplied by Admin CP on init (D1)."""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


def slugify(name: str) -> str:
    """lowercase, non-alphanumeric -> '-', collapse repeats, strip ends. Mirrors Admin CP's
    helper — Org CP's own tenant record has no stored slug column, so the summary view derives
    one from the name it already has, exactly as Admin CP would for the same name."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


class OrgInit(BaseModel):
    id: uuid.UUID
    name: str = Field(min_length=1)


class OrgOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    status: str
    settings: dict
    created_at: datetime
    updated_at: datetime


class OrgSummaryOrganization(BaseModel):
    name: str
    slug: str
    status: str


class OrgSummaryPlan(BaseModel):
    name: str
    tpm: int
    rpm: int
    quota_monthly_tokens: int


class OrgSummary(BaseModel):
    organization: OrgSummaryOrganization
    user_count: int
    role_count: int
    plan: OrgSummaryPlan
