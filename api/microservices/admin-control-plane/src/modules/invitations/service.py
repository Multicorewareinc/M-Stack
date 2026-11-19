"""Super-admin invitations -- a thin Admin CP -> Org CP proxy (same posture as user_provisioning). The
Org CP owns users, roles and the invitation itself; this layer only resolves the target organization
(creating it first when asked) and forwards the request under the service bearer, attributing it to the
acting super-admin. Error posture mirrors the other Admin CP proxies: a faithful status mapping, and an
unreachable Org CP is an UpstreamError, never a silent success."""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from errors import ConflictError, NotFoundError, UnprocessableError, UpstreamError
from modules.organizations import service as org_service
from modules.organizations.schemas import OrgCreate
from modules.user_provisioning.service import ORG_HEADER

# Issuing an invitation includes the Org CP emailing it (up to its 20 s send deadline), so this one call is
# given more room than the 3 s default meant for quick reads. Without it a normal mail send timed out here and
# the Admin Portal reported "Org CP unavailable" for an invitation that had in fact been created and emailed.
ISSUE_TIMEOUT_SECONDS = 30.0


async def issue_invitation(
    org_client: httpx.AsyncClient,
    org_id: uuid.UUID,
    body: dict,
    actor_headers: dict | None = None,
) -> dict[str, Any]:
    try:
        resp = await org_client.post(
            f"/internal/v1/organizations/{org_id}/invitations",
            json=body,
            headers={ORG_HEADER: str(org_id), **(actor_headers or {})},
            timeout=ISSUE_TIMEOUT_SECONDS,
        )
    except httpx.TimeoutException as exc:
        raise UpstreamError(
            "The Org CP took too long to answer. The invitation may have been created; check the list before retrying."
        ) from exc
    except httpx.HTTPError as exc:
        raise UpstreamError("Org CP unavailable") from exc
    if resp.status_code == 201:
        return resp.json()
    detail = _upstream_message(resp)
    if resp.status_code == 409:
        raise ConflictError(detail or "A user with this email already exists", field="email")
    if resp.status_code == 422:
        raise UnprocessableError(detail or "Org CP rejected the invitation (validation)")
    raise UpstreamError(f"Org CP returned {resp.status_code} creating the invitation")


def _upstream_message(resp: httpx.Response) -> str | None:
    try:
        return resp.json()["error"]["message"]
    except Exception:
        return None


async def create_for_existing_org(
    sessionmaker,
    org_client: httpx.AsyncClient,
    org_id: uuid.UUID,
    body: dict,
    actor_headers: dict | None,
) -> dict[str, Any]:
    async with sessionmaker() as session:
        await org_service.get_org(session, org_id)  # 404 if unknown to the Admin CP
    return await issue_invitation(org_client, org_id, body, actor_headers)


async def create_with_new_org(
    sessionmaker,
    org_client: httpx.AsyncClient,
    billing_client: httpx.AsyncClient,
    org_data: OrgCreate,
    body: dict,
    actor_headers: dict | None,
):
    """Create the organization (existing provisioning workflow, incl. default-role seeding), then invite
    into it. Not a distributed transaction: if the invitation is then refused (e.g. the email already has
    an account) the new organization remains, exactly like a failed owner-create in create_org_with_owner.
    """
    org = await org_service.create_org_workflow(sessionmaker, org_client, billing_client, org_data)
    invitation = await issue_invitation(org_client, org.id, body, actor_headers)
    return org, invitation


async def list_invitations(org_client: httpx.AsyncClient, params: dict) -> dict[str, Any]:
    """Pending invitations across every organization (or one, via `organization_id`), straight from the
    Org CP, which owns them. Each item carries `organization_id`; the portal maps it to a name."""
    try:
        resp = await org_client.get("/internal/v1/invitations", params=params)
    except httpx.HTTPError as exc:
        raise UpstreamError("Org CP unavailable") from exc
    if resp.status_code != 200:
        raise UpstreamError(f"Org CP returned {resp.status_code} listing invitations")
    return resp.json()


async def revoke_invitation(
    org_client: httpx.AsyncClient, invitation_id: uuid.UUID, actor_headers: dict | None = None
) -> None:
    try:
        resp = await org_client.delete(
            f"/internal/v1/invitations/{invitation_id}", headers=actor_headers or {}
        )
    except httpx.HTTPError as exc:
        raise UpstreamError("Org CP unavailable") from exc
    if resp.status_code == 204:
        return
    detail = _upstream_message(resp)
    if resp.status_code == 404:
        raise NotFoundError(detail or "Invitation not found")
    if resp.status_code == 409:
        raise ConflictError(detail or "This invitation was already used or revoked")
    raise UpstreamError(f"Org CP returned {resp.status_code} revoking the invitation")
