"""Public `/api/api-keys` router — end-user JWT surface (like /api/auth), NOT behind
require_service_key / require_org_context (ADR-027). Thin handlers: `org_id`/`owner_id` come from the
authenticated principal only; all logic lives in service. Mutations are gated by `apikey.manage`.
"""

from __future__ import annotations

import uuid

from dependencies import require_mg_service_key
from fastapi import APIRouter, Depends, Query, Request, Response, status

from modules.auth.dependencies import get_current_user
from modules.users.models import User

from . import service
from .dependencies import require_apikey_manage
from .schemas import ApiKeyCreate, ApiKeyCreated, ApiKeyOut, ApiKeyUpdate, ApiKeyVerifyOut

router = APIRouter(prefix="/api/api-keys", tags=["api-keys"])


@router.get("", response_model=list[ApiKeyOut])
async def list_keys(request: Request, user: User = Depends(get_current_user)) -> list[ApiKeyOut]:
    return await service.list_keys(request.app.state.sessionmaker, user.organization_id)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiKeyCreated,
    dependencies=[Depends(require_apikey_manage)],
)
async def create_key(
    body: ApiKeyCreate, request: Request, user: User = Depends(get_current_user)
) -> ApiKeyCreated:
    return await service.create_key(
        request.app.state.sessionmaker,
        request.app.state.settings,
        user.organization_id,
        user.id,
        body,
    )


@router.post(
    "/{key_id}/rotate",
    response_model=ApiKeyCreated,
    dependencies=[Depends(require_apikey_manage)],
)
async def rotate_key(
    key_id: uuid.UUID, request: Request, user: User = Depends(get_current_user)
) -> ApiKeyCreated:
    return await service.rotate_key(
        request.app.state.sessionmaker, request.app.state.settings, user.organization_id, key_id
    )


@router.patch(
    "/{key_id}",
    response_model=ApiKeyOut,
    dependencies=[Depends(require_apikey_manage)],
)
async def update_key(
    key_id: uuid.UUID,
    body: ApiKeyUpdate,
    request: Request,
    user: User = Depends(get_current_user),
) -> ApiKeyOut:
    return await service.update_key(
        request.app.state.sessionmaker,
        request.app.state.settings,
        user.organization_id,
        key_id,
        body,
    )


@router.delete(
    "/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_apikey_manage)],
)
async def revoke_key(
    key_id: uuid.UUID, request: Request, user: User = Depends(get_current_user)
) -> Response:
    await service.revoke_key(
        request.app.state.sessionmaker, request.app.state.settings, user.organization_id, key_id
    )
    return Response(status_code=204)


# Internal verify surface for the model-gateway (SP-02, ADR-026). Behind `require_mg_service_key` —
# a bearer DISTINCT from and narrower than the broad `service_api_key` used by /v1 (least-privilege:
# the internet-adjacent model-gateway must not hold a credential that also opens /v1 users/roles/rbac).
# The org is NOT taken from a header here — a key hash resolves to its own org. Returns the minimal
# record or an opaque 404.
internal_router = APIRouter(
    prefix="/internal/v1/api-keys", tags=["api-keys-internal"],
    dependencies=[Depends(require_mg_service_key)],
)


@internal_router.get("/verify", response_model=ApiKeyVerifyOut)
async def verify(request: Request, hash: str = Query(..., min_length=1)) -> ApiKeyVerifyOut:
    return await service.verify_by_hash(request.app.state.sessionmaker, hash)
