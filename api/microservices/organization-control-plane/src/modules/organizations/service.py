"""Organization tenant business logic. All DB access here; services flush() but never commit()
(get_session owns the transaction, design D3). Init is idempotent on the supplied id (D2) so
the Admin CP → Org CP org-creation call (SP-07) is retry-safe."""

from __future__ import annotations

import uuid

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from errors import NotFoundError, UpstreamError
from modules.roles.models import Role, RolePermission
from modules.users.models import User

from .models import Organization
from .schemas import OrgInit, OrgSummary, OrgSummaryOrganization, OrgSummaryPlan, slugify

# Default org-scoped system roles seeded at provisioning (ADR-029/AD-04). Permission ids are
# resolved by slug from the Admin CP master catalog (ADR-019 — no cross-DB FK); the slug set here
# must exist in that catalog (Admin CP seed.py), else seeding fails closed. org_user is read-only.
DEFAULT_ROLE_SLUGS: dict[str, tuple[str, ...]] = {
    "org_admin": (
        "users.read", "users.create", "users.update", "users.delete",
        "roles.read", "roles.create", "roles.update", "roles.delete",
        "organizations.read", "organizations.update", "apikey.manage",
    ),
    "org_user": ("users.read", "roles.read", "organizations.read"),
}


async def init_org(session: AsyncSession, data: OrgInit) -> tuple[Organization, bool]:
    """Create the tenant row for the supplied global id. Idempotent: if the row already exists,
    return it unchanged with created=False (no duplicate, no error). Returns (org, created)."""
    existing = await session.get(Organization, data.id)
    if existing is not None:
        return existing, False
    org = Organization(id=data.id, name=data.name, status="active", settings={})
    session.add(org)
    await session.flush()
    return org, True


async def _catalog_slug_map(admin_client: httpx.AsyncClient) -> dict[str, uuid.UUID]:
    """Fetch the Admin CP master permission catalog once and map slug -> id (ADR-018/019). Fails
    closed (UpstreamError) if Admin CP is unreachable or returns non-200 — the caller adds no role
    rows in that case, so no partial/unbound roles persist."""
    try:
        resp = await admin_client.get("/internal/v1/permissions")
    except httpx.HTTPError as exc:
        raise UpstreamError("Admin CP unavailable") from exc
    if resp.status_code != 200:
        raise UpstreamError(f"Admin CP returned {resp.status_code} fetching permissions")
    return {p["slug"]: uuid.UUID(str(p["id"])) for p in resp.json()}


async def seed_default_roles(
    session: AsyncSession, admin_client: httpx.AsyncClient, org_id: uuid.UUID
) -> None:
    """Insert-missing the default org roles (org_admin, org_user) and their permission bindings for
    org_id. Idempotent by (organization_id, name) + (role_id, permission_id): a retried/duplicate
    provisioning creates no duplicates. Resolves all ids in ONE catalog fetch and fails closed
    (before adding any row) if a required slug is absent — so a role is never bound to an unknown
    permission and no partial roles are left behind (ADR-029/AD-04)."""
    slug_map = await _catalog_slug_map(admin_client)
    needed = {slug for slugs in DEFAULT_ROLE_SLUGS.values() for slug in slugs}
    missing = needed - slug_map.keys()
    if missing:  # fail closed BEFORE any write — no partial/unbound roles
        raise UpstreamError(f"Admin CP catalog missing permissions: {sorted(missing)}")

    existing = {
        r.name: r
        for r in await session.scalars(select(Role).where(Role.organization_id == org_id))
    }
    for name, slugs in DEFAULT_ROLE_SLUGS.items():
        role = existing.get(name)
        if role is None:
            role = Role(organization_id=org_id, name=name, is_system_role=True)
            session.add(role)
            await session.flush()  # assign role.id for the bindings below
        have = set(
            await session.scalars(
                select(RolePermission.permission_id).where(RolePermission.role_id == role.id)
            )
        )
        for slug in slugs:
            pid = slug_map[slug]
            if pid not in have:
                session.add(RolePermission(role_id=role.id, permission_id=pid))
    await session.flush()


async def get_org(session: AsyncSession, org_id: uuid.UUID) -> Organization:
    org = await session.get(Organization, org_id)
    if org is None:
        raise NotFoundError(f"Organization {org_id} not found")
    return org


async def _get_org_plan(admin_client: httpx.AsyncClient, org_id: uuid.UUID) -> OrgSummaryPlan:
    """Plan entitlement is Admin CP-owned (§55, §100) — resolved via internal call, never stored
    here (ADR-018)."""
    try:
        resp = await admin_client.get(f"/internal/v1/organizations/{org_id}/plan")
    except httpx.HTTPError as exc:
        raise UpstreamError("Admin CP unavailable") from exc
    if resp.status_code != 200:
        raise UpstreamError(f"Admin CP returned {resp.status_code} fetching plan")
    body = resp.json()
    return OrgSummaryPlan(
        name=body["name"],
        tpm=body["tpm"],
        rpm=body["rpm"],
        quota_monthly_tokens=body["quota_monthly_tokens"],
    )


async def get_org_summary(
    session: AsyncSession, admin_client: httpx.AsyncClient, org_id: uuid.UUID
) -> OrgSummary:
    org = await get_org(session, org_id)
    user_count = await session.scalar(
        select(func.count()).select_from(User).where(User.organization_id == org_id)
    )
    role_count = await session.scalar(
        select(func.count()).select_from(Role).where(Role.organization_id == org_id)
    )
    plan = await _get_org_plan(admin_client, org_id)
    return OrgSummary(
        organization=OrgSummaryOrganization(name=org.name, slug=slugify(org.name), status=org.status),
        user_count=user_count or 0,
        role_count=role_count or 0,
        plan=plan,
    )
