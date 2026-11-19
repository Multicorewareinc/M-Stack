"""Pydantic v2 DTOs for the audit-log read surface."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID | None
    actor_type: str
    actor_id: str | None
    actor_email: str | None
    action: str
    resource_type: str
    resource_id: str | None
    resource_name: str | None
    details: dict
    ip_address: str | None
    created_at: datetime


class AuditLogPage(BaseModel):
    items: list[AuditLogOut]
    total: int
    limit: int
    offset: int
