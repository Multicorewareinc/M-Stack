"""Organizations business logic. All DB access here; services flush() but never commit()
(get_session owns the transaction, design D3). Referential rules are app-level (AD-06)."""

from __future__ import annotations

import logging
import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from errors import ConflictError, NotFoundError, UnprocessableError, UpstreamError
from modules.plans.models import Plan

from .models import Organization
from .schemas import OrgCreate, OrgOut, OrgUpdate, slugify

logger = logging.getLogger(__name__)


def to_org_out(org: Organization, user_count: int = 0) -> OrgOut:
    return OrgOut.model_validate(org).model_copy(
        update={"user_count": user_count, "retryable": org.status == "failed"}
    )


async def _user_counts(org_client: httpx.AsyncClient, org_ids: list[uuid.UUID]) -> dict[str, int]:
    """Bulk-fetches Org CP's user counts for every listed org in one call (§184 — never one
    request per row). Fails closed (502), matching the user_directory proxy's convention, rather
    than silently showing a misleadingly-low count when Org CP is unreachable."""
    if not org_ids:
        return {}
    try:
        resp = await org_client.get(
            "/internal/v1/organizations/user-counts", params=[("ids", str(i)) for i in org_ids]
        )
    except httpx.HTTPError as exc:
        raise UpstreamError("Org CP unavailable") from exc
    if resp.status_code != 200:
        raise UpstreamError(f"Org CP returned {resp.status_code} fetching user counts")
    return resp.json()["counts"]


async def _require_plan(session: AsyncSession, plan_id: uuid.UUID) -> Plan:
    """422 when the referenced plan does not exist (app-level, not DB-FK — AD-06). Returns the
    loaded Plan so callers that also need one of its fields (e.g. stripe_price_id, for the
    billing subscription notification) don't need a second query — existing call sites that
    only validated existence simply discard the return value, unchanged."""
    plan = await session.get(Plan, plan_id)
    if plan is None:
        raise UnprocessableError(f"Plan {plan_id} does not exist")
    return plan


async def _notify_billing_subscription(
    billing_client: httpx.AsyncClient, org_id: uuid.UUID, stripe_price_id: str | None
) -> None:
    """Best-effort notification to billing's internal subscription endpoint (ADR-032/AD-01,
    add-billing-plan-subscription-linkage) — NEVER raises. A failure here must never fail or
    alter the org-creation/plan-update response; it is logged and swallowed. billing itself
    decides what to do with a null stripe_price_id (an inert no-op, not an error)."""
    try:
        resp = await billing_client.post(
            "/internal/v1/subscriptions",
            json={"org_id": str(org_id), "stripe_price_id": stripe_price_id},
        )
        if resp.status_code != 200:
            logger.warning(
                "billing_subscription_notify_failed",
                extra={"org_id": str(org_id), "status": resp.status_code},
            )
    except httpx.HTTPError:
        logger.warning("billing_subscription_notify_failed", extra={"org_id": str(org_id)})


async def _flush_unique(session: AsyncSession, name: str, slug: str) -> None:
    """Flush, converting a DB UNIQUE(name|slug) violation into a clean 409 — race-free (the DB
    constraint is the source of truth, not a pre-check SELECT). get_session owns rollback (D3)."""
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ConflictError(
            f"Organization name '{name}' (slug '{slug}') already exists", field="name"
        ) from exc


async def create_org(session: AsyncSession, data: OrgCreate) -> Organization:
    await _require_plan(session, data.plan_id)
    slug = slugify(data.name)
    org = Organization(name=data.name, slug=slug, plan_id=data.plan_id, status="active")
    session.add(org)
    await _flush_unique(session, data.name, slug)
    return org


async def _init_org_cp(org_client: httpx.AsyncClient, org_id: uuid.UUID, org_name: str) -> bool:
    """Calls Org CP's idempotent init (201 create / 200 re-init on the same id). Returns True on
    failure (non-2xx or unreachable) — never raises, the caller decides how to persist that."""
    try:
        resp = await org_client.post(
            "/internal/v1/organizations", json={"id": str(org_id), "name": org_name}
        )
        return resp.status_code not in (200, 201)
    except httpx.HTTPError:
        return True


async def create_org_workflow(
    sessionmaker: async_sessionmaker[AsyncSession],
    org_client: httpx.AsyncClient,
    billing_client: httpx.AsyncClient,
    data: OrgCreate,
) -> Organization:
    """Cross-control-plane org creation (§11, §53, §73). Two local transactions around one
    synchronous Org CP call — NOT a single get_session transaction (D1), so a failed Org CP init
    leaves a durable `failed` org rather than rolling everything back.

    txn1: validate plan (422) + insert `provisioning` (dup → 409), commit.
    call: Org CP init with the same global id (outside any DB txn).
    txn2: set `active` (2xx) or `failed` (error/non-2xx), commit; raise 502 on failure.
    Then (only when active): best-effort-notify billing of the org's plan price (ADR-032).
    """
    # txn1 — provision. (expire_on_commit=False keeps org/plan attributes readable after commit.)
    async with sessionmaker() as session:
        plan = await _require_plan(session, data.plan_id)
        slug = slugify(data.name)
        org = Organization(name=data.name, slug=slug, plan_id=data.plan_id, status="provisioning")
        session.add(org)
        await _flush_unique(session, data.name, slug)
        await session.commit()
        org_id, org_name = org.id, org.name

    # cross-service init (no distributed transaction, §73).
    failed = await _init_org_cp(org_client, org_id, org_name)

    # txn2 — terminal status.
    async with sessionmaker() as session:
        org = await session.get(Organization, org_id)
        org.status = "failed" if failed else "active"
        await session.commit()
        result = org  # detached after the block; attrs stay loaded (expire_on_commit=False)

    if failed:
        raise UpstreamError(f"Org CP initialization failed for organization {org_id}")

    # Best-effort billing notification (ADR-032) — only for an org that actually became active;
    # a Subscription for a nonexistent/failed org makes no sense. `plan` was loaded in txn1's
    # now-closed session; its scalar `stripe_price_id` attribute stays readable (expire_on_commit
    # =False), so no second query is needed.
    await _notify_billing_subscription(billing_client, org_id, plan.stripe_price_id)
    return result


async def retry_org_workflow(
    sessionmaker: async_sessionmaker[AsyncSession],
    org_client: httpx.AsyncClient,
    billing_client: httpx.AsyncClient,
    org_id: uuid.UUID,
) -> Organization:
    """Re-attempts Org CP init for a `failed` organization (§92, §114) — the only status this is
    valid from; a non-failed org is never retryable (409), mirroring create's terminal-status
    pattern. Org CP's init is idempotent on the id, so this is safe even if a prior attempt
    partially succeeded there."""
    async with sessionmaker() as session:
        org = await get_org(session, org_id)
        if org.status != "failed":
            raise ConflictError(f"Organization {org_id} is not in a failed state")
        org.status = "provisioning"
        await session.commit()
        org_name, plan_id = org.name, org.plan_id

    failed = await _init_org_cp(org_client, org_id, org_name)

    async with sessionmaker() as session:
        org = await session.get(Organization, org_id)
        org.status = "failed" if failed else "active"
        await session.commit()
        result = org

    if failed:
        raise UpstreamError(f"Org CP initialization failed for organization {org_id}")

    # A successful retry is the SAME "org created with a plan" moment as create_org_workflow's
    # own success path (the org never became active on the original attempt) — notify billing
    # here too (ADR-032), or an org that needed a retry would silently never get a subscription.
    async with sessionmaker() as session:
        plan = await session.get(Plan, plan_id)
    await _notify_billing_subscription(billing_client, org_id, plan.stripe_price_id if plan else None)
    return result


async def list_orgs(
    session: AsyncSession, org_client: httpx.AsyncClient, search: str = ""
) -> list[OrgOut]:
    # ponytail: unpaginated list; add limit/offset when org counts grow past a screenful.
    query = select(Organization).order_by(Organization.created_at)
    if search:
        query = query.where(Organization.name.ilike(f"%{search}%"))
    result = await session.scalars(query)
    orgs = list(result)
    counts = await _user_counts(org_client, [o.id for o in orgs])
    return [to_org_out(o, counts.get(str(o.id), 0)) for o in orgs]


async def _get_org_model(session: AsyncSession, org_id: uuid.UUID) -> Organization:
    org = await session.get(Organization, org_id)
    if org is None:
        raise NotFoundError(f"Organization {org_id} not found")
    return org


async def get_org(session: AsyncSession, org_id: uuid.UUID) -> Organization:
    return await _get_org_model(session, org_id)


async def get_org_detail(
    session: AsyncSession, org_client: httpx.AsyncClient, org_id: uuid.UUID
) -> OrgOut:
    org = await _get_org_model(session, org_id)
    counts = await _user_counts(org_client, [org.id])
    return to_org_out(org, counts.get(str(org.id), 0))


async def update_org(
    session: AsyncSession, billing_client: httpx.AsyncClient, org_id: uuid.UUID, data: OrgUpdate
) -> Organization:
    org = await _get_org_model(session, org_id)
    fields = data.model_dump(exclude_unset=True)

    new_plan = None
    if "plan_id" in fields and fields["plan_id"] != org.plan_id:
        new_plan = await _require_plan(session, fields["plan_id"])

    new_name = fields.get("name", org.name)
    new_slug = slugify(new_name) if "name" in fields else org.slug
    if "name" in fields:
        org.slug = new_slug

    for key, value in fields.items():
        setattr(org, key, value)
    await _flush_unique(session, new_name, new_slug)

    # Best-effort billing notification (ADR-032) — only when the plan actually changed. `session`
    # is the SAME single request-scoped session for this whole function, so `new_plan` is never
    # detached (unlike create_org_workflow's cross-transaction case).
    if new_plan is not None:
        await _notify_billing_subscription(billing_client, org_id, new_plan.stripe_price_id)
    return org


async def delete_org(session: AsyncSession, org_id: uuid.UUID) -> None:
    org = await _get_org_model(session, org_id)
    await session.delete(org)
    await session.flush()


async def get_org_plan(session: AsyncSession, org_id: uuid.UUID) -> Plan:
    """For Org CP's organization-summary proxy call (ADR-018) — resolves an org's plan without
    Org CP ever holding plan data itself (plans are Admin CP-owned)."""
    org = await _get_org_model(session, org_id)
    plan = await session.get(Plan, org.plan_id)
    if plan is None:
        raise NotFoundError(f"Plan {org.plan_id} not found")
    return plan
