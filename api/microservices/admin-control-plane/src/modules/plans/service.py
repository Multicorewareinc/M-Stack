"""Plans business logic. All DB access lives here; the router is thin. Services flush() but
never commit() — the get_session dependency owns the transaction (design D3)."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from errors import ConflictError, NotFoundError
from modules.organizations.models import Organization

from .models import Plan
from .schemas import PlanCreate, PlanUpdate


async def _flush_unique(session: AsyncSession, name: str) -> None:
    """Flush, converting the DB UNIQUE(name) violation into a clean 409. Letting the DB be the
    source of truth (rather than a pre-check SELECT) is race-free — a concurrent duplicate
    insert still yields 409, never a 500. get_session owns the rollback (design D3)."""
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ConflictError(f"Plan name '{name}' already exists", field="name") from exc


async def create_plan(session: AsyncSession, data: PlanCreate) -> Plan:
    plan = Plan(
        name=data.name,
        tpm=data.tpm,
        rpm=data.rpm,
        quota_monthly_tokens=data.quota_monthly_tokens,
        stripe_price_id=data.stripe_price_id,
        is_default=False,
    )
    session.add(plan)
    await _flush_unique(session, data.name)
    return plan


async def list_plans(session: AsyncSession) -> list[Plan]:
    # ponytail: unpaginated list; add limit/offset when plan counts grow past a screenful.
    result = await session.scalars(select(Plan).order_by(Plan.created_at))
    return list(result)


async def get_plan(session: AsyncSession, plan_id: uuid.UUID) -> Plan:
    plan = await session.get(Plan, plan_id)
    if plan is None:
        raise NotFoundError(f"Plan {plan_id} not found")
    return plan


async def update_plan(session: AsyncSession, plan_id: uuid.UUID, data: PlanUpdate) -> Plan:
    plan = await get_plan(session, plan_id)
    fields = data.model_dump(exclude_unset=True)
    for key, value in fields.items():
        setattr(plan, key, value)
    await _flush_unique(session, fields.get("name", plan.name))
    return plan


async def delete_plan(session: AsyncSession, plan_id: uuid.UUID) -> None:
    plan = await get_plan(session, plan_id)
    # Referential rule (AD-06): block delete if any org references this plan. App-level check
    # is authoritative; the DB FK ON DELETE RESTRICT is a Postgres backstop.
    referencing = await session.scalar(
        select(func.count()).select_from(Organization).where(Organization.plan_id == plan_id)
    )
    if referencing:
        raise ConflictError(f"Plan {plan_id} is referenced by {referencing} organization(s)")
    await session.delete(plan)
    await session.flush()
