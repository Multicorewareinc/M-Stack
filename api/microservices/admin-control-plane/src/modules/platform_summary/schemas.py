"""Pydantic v2 DTO for the platform-wide dashboard summary (Admin Portal overview, §125 no
charts — counts + status breakdown only)."""

from __future__ import annotations

from pydantic import BaseModel


class PlatformSummary(BaseModel):
    organizations: int
    active_users: int
    plans: int
    provisioning: int
    active: int
    failed: int
