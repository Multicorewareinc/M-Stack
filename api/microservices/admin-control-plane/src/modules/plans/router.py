"""Plans HTTP routes. Thin: parse, delegate to service, return. Auth is enforced at router
level (require_admin_key); no business logic here."""

from __future__ import annotations

import uuid

from db import get_session
from dependencies import require_admin_key
from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from modules.audit import service as audit

from . import service
from .schemas import PlanCreate, PlanOut, PlanUpdate

router = APIRouter(prefix="/v1/plans", tags=["plans"], dependencies=[Depends(require_admin_key)])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=PlanOut)
async def create(
    body: PlanCreate,
    request: Request,
    session: AsyncSession = Depends(get_session, scope="function"),
) -> PlanOut:
    plan = await service.create_plan(session, body)
    await audit.record(
        session,
        action="plan.created",
        resource_type="plan",
        resource_id=plan.id,
        resource_name=plan.name,
        **audit.ctx(request),
    )
    return plan


@router.get("", response_model=list[PlanOut])
async def list_all(session: AsyncSession = Depends(get_session, scope="function")) -> list[PlanOut]:
    return await service.list_plans(session)


@router.get("/{plan_id}", response_model=PlanOut)
async def get_one(
    plan_id: uuid.UUID, session: AsyncSession = Depends(get_session, scope="function")
) -> PlanOut:
    return await service.get_plan(session, plan_id)


@router.patch("/{plan_id}", response_model=PlanOut)
async def patch(
    plan_id: uuid.UUID,
    body: PlanUpdate,
    request: Request,
    session: AsyncSession = Depends(get_session, scope="function"),
) -> PlanOut:
    plan = await service.update_plan(session, plan_id, body)
    await audit.record(
        session,
        action="plan.updated",
        resource_type="plan",
        resource_id=plan_id,
        resource_name=plan.name,
        details={"changed": sorted(body.model_dump(exclude_unset=True))},
        **audit.ctx(request),
    )
    return plan


@router.delete("/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete(
    plan_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session, scope="function"),
) -> None:
    name = (await service.get_plan(session, plan_id)).name  # 404s here exactly as delete would
    await service.delete_plan(session, plan_id)
    await audit.record(
        session,
        action="plan.deleted",
        resource_type="plan",
        resource_id=plan_id,
        resource_name=name,
        **audit.ctx(request),
    )
