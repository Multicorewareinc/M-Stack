"""Plans HTTP routes. Thin: parse, delegate to service, return. Auth is enforced at router
level (require_admin_key); no business logic here."""

from __future__ import annotations

import uuid

from db import get_session
from dependencies import require_admin_key
from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from . import service
from .schemas import PlanCreate, PlanOut, PlanUpdate

router = APIRouter(prefix="/v1/plans", tags=["plans"], dependencies=[Depends(require_admin_key)])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=PlanOut)
async def create(body: PlanCreate, session: AsyncSession = Depends(get_session)) -> PlanOut:
    return await service.create_plan(session, body)


@router.get("", response_model=list[PlanOut])
async def list_all(session: AsyncSession = Depends(get_session)) -> list[PlanOut]:
    return await service.list_plans(session)


@router.get("/{plan_id}", response_model=PlanOut)
async def get_one(plan_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> PlanOut:
    return await service.get_plan(session, plan_id)


@router.patch("/{plan_id}", response_model=PlanOut)
async def patch(
    plan_id: uuid.UUID, body: PlanUpdate, session: AsyncSession = Depends(get_session)
) -> PlanOut:
    return await service.update_plan(session, plan_id, body)


@router.delete("/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete(plan_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> None:
    await service.delete_plan(session, plan_id)
