"""Users HTTP routes. Public `/v1/users` are tenant-scoped via require_org_context; internal
`/internal/v1/organizations/{organization_id}/users` take the org from the path (Admin CP
supplies it). Both under the service bearer. Thin handlers; all scoping/logic in service."""

from __future__ import annotations

import uuid

from db import get_session
from dependencies import require_org_context, require_service_key
from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from modules.audit import service as audit
from modules.rbac.dependencies import require_permission
from modules.users.models import User

from . import service
from .schemas import ActiveUserCount, UserCounts, UserCreate, UserOut, UserUpdate

router = APIRouter(prefix="/v1/users", tags=["users"], dependencies=[Depends(require_service_key)])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=UserOut)
async def create(
    body: UserCreate,
    request: Request,
    org_id: uuid.UUID = Depends(require_org_context),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> UserOut:
    user = await service.create_user(session, org_id, body)
    await _audit_created(session, request, org_id, user)
    return user


async def _audit_created(
    session: AsyncSession, request: Request, org_id: uuid.UUID, user: User, actor=None
) -> None:
    await audit.record(
        session,
        organization_id=org_id,
        action="user.created",
        resource_type="user",
        resource_id=user.id,
        resource_name=user.email,
        details={"username": user.username},
        **audit.ctx(request, actor),
    )


@router.get("", response_model=list[UserOut])
async def list_all(
    org_id: uuid.UUID = Depends(require_org_context),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[UserOut]:
    return await service.list_users(session, org_id)


@router.get("/{user_id}", response_model=UserOut)
async def get_one(
    user_id: uuid.UUID,
    org_id: uuid.UUID = Depends(require_org_context),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> UserOut:
    return await service.get_user(session, org_id, user_id)


@router.patch("/{user_id}", response_model=UserOut)
async def patch(
    user_id: uuid.UUID,
    body: UserUpdate,
    request: Request,
    org_id: uuid.UUID = Depends(require_org_context),
) -> UserOut:
    # Owns its own transaction (so a deactivation can evict the user's key cache post-commit, ADR-026).
    user = await service.update_user(
        request.app.state.sessionmaker, request.app.state.settings, org_id, user_id, body
    )
    fields = body.model_dump(exclude_unset=True)
    context = audit.ctx(request)
    sessionmaker = request.app.state.sessionmaker
    changed = sorted(k for k in fields if k != "password")
    if changed:
        details: dict = {"changed": changed}
        if "status" in fields:
            details["status"] = fields["status"]
        await audit.record_detached(
            sessionmaker,
            organization_id=org_id,
            action="user.updated",
            resource_type="user",
            resource_id=user_id,
            resource_name=user.email,
            details=details,
            **context,
        )
    if "password" in fields:
        await audit.record_detached(
            sessionmaker,
            organization_id=org_id,
            action="user.password_reset",
            resource_type="user",
            resource_id=user_id,
            resource_name=user.email,
            **context,
        )
    return user


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete(
    user_id: uuid.UUID,
    request: Request,
    org_id: uuid.UUID = Depends(require_org_context),
) -> None:
    # Owns its own transaction (revoke keys + delete + post-commit evict, SP-03) — not the per-request
    # get_session, so eviction is genuinely post-commit.
    sessionmaker = request.app.state.sessionmaker
    async with (
        sessionmaker() as session
    ):  # name for the audit row; 404s here exactly as delete would
        email = (await service.get_user(session, org_id, user_id)).email
    await service.delete_user(sessionmaker, request.app.state.settings, org_id, user_id)
    await audit.record_detached(
        sessionmaker,
        organization_id=org_id,
        action="user.deleted",
        resource_type="user",
        resource_id=user_id,
        resource_name=email,
        **audit.ctx(request),
    )


# Identity + RBAC-gated end-user surface (ADR-029/AD-01,AD-02a). NOT behind require_service_key —
# the caller is an authenticated org user (JWT), and the org is taken from the verified token, never
# a client header. This is the first route to consume the rbac can() seam.
api_router = APIRouter(prefix="/api/users", tags=["users"])


@api_router.post("", status_code=status.HTTP_201_CREATED, response_model=UserOut)
async def create_as_org_admin(
    body: UserCreate,
    request: Request,
    actor: User = Depends(require_permission("users.create")),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> UserOut:
    # Org is the caller's own (from the verified token), never a client header — tenant isolation.
    user = await service.create_user(session, actor.organization_id, body)
    await service.assign_default_role(session, actor.organization_id, user.id, "org_user")
    await _audit_created(
        session, request, actor.organization_id, user, audit.Actor.from_user(actor)
    )
    return user


# Internal read surface for the Admin CP proxy (SP-06). Org comes from the path.
internal_router = APIRouter(
    prefix="/internal/v1/organizations",
    tags=["users-internal"],
    dependencies=[Depends(require_service_key)],
)


@internal_router.get("/user-counts", response_model=UserCounts)
async def internal_user_counts(
    ids: list[uuid.UUID] = Query(default=[]),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> UserCounts:
    return UserCounts(counts=await service.count_users_by_org(session, ids))


@internal_router.get("/active-user-count", response_model=ActiveUserCount)
async def internal_active_user_count(
    session: AsyncSession = Depends(get_session, scope="function"),
) -> ActiveUserCount:
    return ActiveUserCount(count=await service.count_active_users_total(session))


@internal_router.get("/{organization_id}/users", response_model=list[UserOut])
async def internal_list(
    organization_id: uuid.UUID, session: AsyncSession = Depends(get_session, scope="function")
) -> list[UserOut]:
    return await service.list_users(session, organization_id)


@internal_router.get("/{organization_id}/users/{user_id}", response_model=UserOut)
async def internal_get(
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    session: AsyncSession = Depends(get_session, scope="function"),
) -> UserOut:
    return await service.get_user(session, organization_id, user_id)


# Platform-wide surface for Admin CP's flat user-directory proxy (Super-Admin only). A
# deliberately separate prefix from /internal/v1/organizations — these are the one place a user
# is resolved without an org already known, not a tenant operation (§45 is about /v1 routes).
platform_router = APIRouter(
    prefix="/internal/v1/users",
    tags=["users-internal"],
    dependencies=[Depends(require_service_key)],
)


@platform_router.get("", response_model=list[UserOut])
async def platform_list(
    session: AsyncSession = Depends(get_session, scope="function"),
) -> list[UserOut]:
    return await service.list_all_users(session)


@platform_router.get("/{user_id}", response_model=UserOut)
async def platform_get(
    user_id: uuid.UUID, session: AsyncSession = Depends(get_session, scope="function")
) -> UserOut:
    return await service.get_user_by_id(session, user_id)
