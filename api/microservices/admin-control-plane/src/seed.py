"""Idempotent seed of the three default plan tiers, run once on lifespan startup (gated by
SEED_PLANS). Insert-missing-by-name only: a tier whose name already exists is left untouched,
so a super-admin's later edits survive a reboot (AD-04)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.permissions.models import Permission
from modules.permissions.schemas import slug_for
from modules.plans.models import Plan

# name -> (tpm, rpm, quota_monthly_tokens). 0 = unlimited.
DEFAULT_PLANS: dict[str, tuple[int, int, int]] = {
    "Free": (10000, 60, 1000000),
    "Pro": (100000, 600, 50000000),
    "Enterprise": (0, 0, 0),
}

# (resource, action) — the default master permission catalog (rbac doc §26, ADR-019).
DEFAULT_PERMISSIONS: list[tuple[str, str]] = [
    ("users", "read"),
    ("users", "create"),
    ("users", "update"),
    ("users", "delete"),
    ("roles", "read"),
    ("roles", "create"),
    ("roles", "update"),
    ("roles", "delete"),
    ("organizations", "read"),
    ("organizations", "update"),
    # API-key lifecycle mutation gate — org roles compose this to allow key create/rotate/revoke
    # (ADR-027; enforced server-side in org-CP via the RBAC can() seam).
    ("apikey", "manage"),
]


async def seed_plans(session: AsyncSession, enabled: bool = True) -> None:
    if not enabled:
        return
    existing = set(await session.scalars(select(Plan.name)))
    added = False
    for name, (tpm, rpm, quota) in DEFAULT_PLANS.items():
        if name in existing:
            continue  # insert-missing only — never overwrite edited values
        session.add(
            Plan(name=name, tpm=tpm, rpm=rpm, quota_monthly_tokens=quota, is_default=True)
        )
        added = True
    if added:
        await session.commit()


async def seed_permissions(session: AsyncSession, enabled: bool = True) -> None:
    """Seed the default master permission catalog. Insert-missing by `slug` only — a Super-Admin's
    later edits (description, is_active) survive a reboot. Mirrors seed_plans (ADR-019/ADR-015)."""
    if not enabled:
        return
    existing = set(await session.scalars(select(Permission.slug)))
    added = False
    for resource, action in DEFAULT_PERMISSIONS:
        slug = slug_for(resource, action)
        if slug in existing:
            continue  # insert-missing only — never overwrite edited values
        session.add(Permission(resource=resource, action=action, slug=slug))
        added = True
    if added:
        await session.commit()
