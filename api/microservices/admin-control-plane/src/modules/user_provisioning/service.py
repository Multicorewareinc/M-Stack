"""Super-admin user provisioning (ADR-029/AD-02b,AD-02c) — a thin Admin CP -> Org CP proxy. No DB
writes of its own beyond the existing org-creation workflow: it creates the user on the Org CP
(service-key client + X-Organization-Id) and assigns the org's seeded `org_admin` role (SP-01).

Error posture mirrors the existing Admin CP proxies (user_directory / _user_counts): map the Org CP
status to a clean Admin CP error; an unreachable Org CP is an UpstreamError (never a silent success).
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from errors import ConflictError, UnprocessableError, UpstreamError
from modules.organizations import service as org_service
from modules.organizations.schemas import OrgCreate

ORG_HEADER = "X-Organization-Id"
ORG_ADMIN_ROLE = "org_admin"


async def _org_create_user(
    org_client: httpx.AsyncClient, org_id: uuid.UUID, body: dict
) -> dict[str, Any]:
    """Proxy Org CP POST /v1/users under the target org. Faithful status mapping."""
    try:
        resp = await org_client.post("/v1/users", json=body, headers={ORG_HEADER: str(org_id)})
    except httpx.HTTPError as exc:
        raise UpstreamError("Org CP unavailable") from exc
    if resp.status_code == 201:
        return resp.json()
    if resp.status_code == 409:
        raise ConflictError("User already exists in this organization", field="email")
    if resp.status_code == 422:
        raise UnprocessableError("Org CP rejected the user (validation)")
    raise UpstreamError(f"Org CP returned {resp.status_code} creating the user")


async def _assign_org_admin(
    org_client: httpx.AsyncClient, org_id: uuid.UUID, user_id: uuid.UUID | str
) -> None:
    """Look up the org's seeded org_admin role and assign it to the user (fail-closed on error)."""
    try:
        roles_resp = await org_client.get("/v1/roles", headers={ORG_HEADER: str(org_id)})
        if roles_resp.status_code != 200:
            raise UpstreamError(f"Org CP returned {roles_resp.status_code} listing roles")
        role_id = next(
            (r["id"] for r in roles_resp.json() if r.get("name") == ORG_ADMIN_ROLE), None
        )
        if role_id is None:
            raise UpstreamError("org_admin role is not provisioned for this organization")
        assign = await org_client.put(
            f"/v1/users/{user_id}/roles",
            json={"role_ids": [role_id]},
            headers={ORG_HEADER: str(org_id)},
        )
        if assign.status_code != 200:
            raise UpstreamError(f"Org CP returned {assign.status_code} assigning org_admin")
    except httpx.HTTPError as exc:
        raise UpstreamError("Org CP unavailable") from exc


async def create_user_for_org(
    sessionmaker, org_client: httpx.AsyncClient, org_id: uuid.UUID, owner: dict
) -> dict[str, Any]:
    """Create a login-capable user for an EXISTING org and make them an org_admin. 404 if the org is
    unknown to the Admin CP (no Org CP call attempted)."""
    async with sessionmaker() as session:
        await org_service.get_org(session, org_id)  # 404 if unknown
    user = await _org_create_user(org_client, org_id, owner)
    await _assign_org_admin(org_client, org_id, user["id"])
    return user


async def create_org_with_owner(
    sessionmaker,
    org_client: httpx.AsyncClient,
    billing_client: httpx.AsyncClient,
    org_data: OrgCreate,
    owner: dict,
) -> dict[str, Any]:
    """Create a NEW org (existing workflow: provisions + Org CP init/default-role seed) then its first
    org_admin owner. Returns {organization, owner}. A downstream owner-create failure surfaces as an
    error; the org remains (ADR-029 — not a distributed transaction)."""
    org = await org_service.create_org_workflow(sessionmaker, org_client, billing_client, org_data)
    user = await _org_create_user(org_client, org.id, owner)
    await _assign_org_admin(org_client, org.id, user["id"])
    return {"organization": org_service.to_org_out(org).model_dump(mode="json"), "owner": user}
