"""Invitation routes.

- `POST /api/invitations` -- an org admin (JWT + `users.create`) invites someone into THEIR OWN
  organization; the org comes from the verified token, never the client.
- `POST /internal/v1/organizations/{organization_id}/invitations` -- Admin CP (service bearer) invites
  someone into any organization it names.
- `GET /api/invitations` / `DELETE /api/invitations/{id}` -- the same admin lists their own organization's
  pending invitations and revokes one (JWT + `users.create`; org from the token, never the client).
- `POST /api/invitations/inspect` and `POST /api/invitations/accept` -- PUBLIC (the invitee has no
  account yet). The unguessable one-time token in the body is the credential; accepting creates the
  user with the password they choose.
"""

from __future__ import annotations

import uuid

from db import get_session
from dependencies import require_service_key
from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from modules.audit import service as audit
from modules.rbac.dependencies import require_permission
from modules.users.models import User

from . import mail, service
from .schemas import (
    InvitationAcceptIn,
    InvitationCreate,
    InvitationCreated,
    InvitationInspectIn,
    InvitationInspectOut,
    InvitationOut,
    InvitationPage,
)


async def _commit_before_responding(session: AsyncSession) -> None:
    """Commit NOW rather than leaving it to get_session's cleanup. FastAPI runs a yield-dependency's
    cleanup after the response has been sent, so a client that acts on the response immediately (the
    portals re-list invitations the instant a create/revoke returns; an invitee follows the link at once)
    could otherwise read the state from before this change. get_session's own commit then has nothing
    left to do."""
    await session.commit()


async def _issue(
    session: AsyncSession,
    request: Request,
    org_id: uuid.UUID,
    body: InvitationCreate,
    actor: audit.Actor | None,
) -> InvitationCreated:
    context = audit.ctx(request, actor)
    settings = request.app.state.settings
    inv, org, role, raw = await service.create_invitation(
        session,
        settings,
        org_id,
        email=body.email,
        name=body.name,
        role_id=body.role_id,
        role_name=body.role_name,
        invited_by=context["actor"].email,
    )
    await audit.record(
        session,
        organization_id=org_id,
        action="invitation.created",
        resource_type="invitation",
        resource_id=inv.id,
        resource_name=inv.email,
        details={"role": role.name},
        **context,
    )
    await _commit_before_responding(session)
    path = service.invite_path(raw)
    base = (settings.org_portal_base_url or "").rstrip("/")
    link = f"{base}{path}" if base else None
    # After the commit, so a link that reaches an inbox already works; and never raises: a failed or
    # unconfigured email just means the admin sends the (still valid) link themselves.
    emailed = await mail.send_invitation_email(
        settings,
        to=inv.email,
        org_name=org.name,
        role_name=role.name,
        invited_by=context["actor"].email,
        name=inv.name,
        link=link,
        expires_at=inv.expires_at,
    )
    return InvitationCreated(
        id=inv.id,
        email=inv.email,
        name=inv.name,
        organization_id=org.id,
        organization_name=org.name,
        role_id=role.id,
        role_name=role.name,
        expires_at=inv.expires_at,
        # The link is a live credential: hand it back only when the admin has to deliver it themselves.
        invite_path=path if emailed.status != "sent" else None,
        invite_url=link if emailed.status != "sent" else None,
        email_status=emailed.status,
        email_detail=emailed.detail,
    )


def _page(rows: list, total: int, limit: int, offset: int) -> InvitationPage:
    return InvitationPage(
        items=[
            InvitationOut(
                id=inv.id,
                email=inv.email,
                name=inv.name,
                organization_id=inv.organization_id,
                role_id=inv.role_id,
                role_name=role_name,
                invited_by=inv.invited_by,
                created_at=inv.created_at,
                expires_at=inv.expires_at,
            )
            for inv, role_name in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


async def _revoke(
    session: AsyncSession,
    request: Request,
    org_id: uuid.UUID | None,
    invitation_id: uuid.UUID,
    actor: audit.Actor | None,
) -> None:
    inv = await service.revoke_invitation(session, org_id, invitation_id)
    await audit.record(
        session,
        organization_id=inv.organization_id,
        action="invitation.revoked",
        resource_type="invitation",
        resource_id=inv.id,
        resource_name=inv.email,
        **audit.ctx(request, actor),
    )
    await _commit_before_responding(session)


router = APIRouter(prefix="/api/invitations", tags=["invitations"])


@router.get("", response_model=InvitationPage)
async def list_own_org(
    search: str | None = Query(default=None, max_length=200),
    role: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=25, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    actor: User = Depends(require_permission("users.create")),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> InvitationPage:
    rows, total = await service.list_invitations(
        session, actor.organization_id, search=search, role_name=role, limit=limit, offset=offset
    )
    return _page(rows, total, limit, offset)


@router.delete("/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_own_org(
    invitation_id: uuid.UUID,
    request: Request,
    actor: User = Depends(require_permission("users.create")),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> None:
    await _revoke(
        session, request, actor.organization_id, invitation_id, audit.Actor.from_user(actor)
    )


@router.post("", status_code=status.HTTP_201_CREATED, response_model=InvitationCreated)
async def invite_to_own_org(
    body: InvitationCreate,
    request: Request,
    actor: User = Depends(require_permission("users.create")),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> InvitationCreated:
    return await _issue(session, request, actor.organization_id, body, audit.Actor.from_user(actor))


@router.post("/inspect", response_model=InvitationInspectOut)
async def inspect(
    body: InvitationInspectIn, session: AsyncSession = Depends(get_session, scope="function")
) -> InvitationInspectOut:
    inv, org, role = await service.inspect_invitation(session, body.token)
    return InvitationInspectOut(
        email=inv.email,
        name=inv.name,
        organization_name=org.name,
        role_name=role.name,
        expires_at=inv.expires_at,
    )


@router.post("/accept", status_code=status.HTTP_204_NO_CONTENT)
async def accept(
    body: InvitationAcceptIn,
    request: Request,
    session: AsyncSession = Depends(get_session, scope="function"),
) -> None:
    user, inv = await service.accept_invitation(session, body.token, body.password)
    await audit.record(
        session,
        organization_id=inv.organization_id,
        action="invitation.accepted",
        resource_type="invitation",
        resource_id=inv.id,
        resource_name=inv.email,
        **audit.ctx(request, audit.Actor.from_user(user)),
    )
    await _commit_before_responding(session)


internal_router = APIRouter(
    prefix="/internal/v1/organizations",
    tags=["invitations-internal"],
    dependencies=[Depends(require_service_key)],
)


@internal_router.post(
    "/{organization_id}/invitations",
    status_code=status.HTTP_201_CREATED,
    response_model=InvitationCreated,
)
async def invite_to_org(
    organization_id: uuid.UUID,
    body: InvitationCreate,
    request: Request,
    session: AsyncSession = Depends(get_session, scope="function"),
) -> InvitationCreated:
    return await _issue(session, request, organization_id, body, None)


# Cross-org surface for Admin CP's invitations page. A separate prefix from /internal/v1/organizations:
# listing is not scoped to one organization (an optional `organization_id` narrows it).
platform_router = APIRouter(
    prefix="/internal/v1/invitations",
    tags=["invitations-internal"],
    dependencies=[Depends(require_service_key)],
)


@platform_router.get("", response_model=InvitationPage)
async def list_all_orgs(
    organization_id: uuid.UUID | None = None,
    search: str | None = Query(default=None, max_length=200),
    role: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=25, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session, scope="function"),
) -> InvitationPage:
    rows, total = await service.list_invitations(
        session, organization_id, search=search, role_name=role, limit=limit, offset=offset
    )
    return _page(rows, total, limit, offset)


@platform_router.delete("/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_any_org(
    invitation_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session, scope="function"),
) -> None:
    await _revoke(session, request, None, invitation_id, None)
