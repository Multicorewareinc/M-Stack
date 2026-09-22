"""Mutation gating for API-key routes (ADR-027, reference §9/§12.9).

The `apikey.manage` permission is owned by Admin CP (org-CP has no local `permissions` table — it
deals only in permission UUIDs, validated per-id against Admin CP). Its slug→UUID mapping is resolved
ONCE at startup (design D6) and cached on `app.state.apikey_manage_permission_id`; the gate reads that
cached id and calls the existing RBAC `can()` seam — no per-request Admin CP call on the mint path.
The actual outbound call lives in `rbac.service.fetch_permission_id_by_slug` — no httpx call site
in this module (router → dependencies/service/errors, ADR-003 convention; council review fix — this
used to duplicate the same fetch-by-slug logic `rbac/dependencies.py` also had, each with its own
httpx call from a dependencies module).

Failure posture is fail-closed: if the id is unresolved (Admin CP was unreachable at startup or the
slug is absent), the gate denies (403), never fails open. A lazy one-shot refresh is attempted so a
late-starting Admin CP self-heals without an org-CP reboot.
"""

from __future__ import annotations

import uuid

import httpx
from fastapi import Depends, Request

from errors import ForbiddenError
from modules.auth.dependencies import get_current_user
from modules.rbac.service import can, fetch_permission_id_by_slug
from modules.users.models import User

APIKEY_MANAGE_SLUG = "apikey.manage"


async def load_apikey_manage_permission_id(admin_client: httpx.AsyncClient) -> uuid.UUID | None:
    """Fetch the Admin master catalog and return the id of the `apikey.manage` permission, or None
    if Admin CP is unreachable or the slug is absent (caller treats None as fail-closed). Thin
    wrapper over the shared `rbac.service.fetch_permission_id_by_slug` — kept as a named symbol
    since `main.py` imports it directly for the startup resolve."""
    return await fetch_permission_id_by_slug(admin_client, APIKEY_MANAGE_SLUG)


async def _resolve_id(request: Request) -> uuid.UUID | None:
    perm_id = getattr(request.app.state, "apikey_manage_permission_id", None)
    if perm_id is None:  # lazy one-shot refresh (self-heal a late Admin CP)
        perm_id = await load_apikey_manage_permission_id(request.app.state.admin_client)
        request.app.state.apikey_manage_permission_id = perm_id
    return perm_id


async def require_apikey_manage(
    request: Request, user: User = Depends(get_current_user)
) -> None:
    perm_id = await _resolve_id(request)
    if perm_id is None:
        raise ForbiddenError()  # fail-closed: unresolved permission ⇒ deny
    async with request.app.state.sessionmaker() as session:
        allowed = await can(session, user.organization_id, user.id, perm_id)
    if not allowed:
        raise ForbiddenError()
