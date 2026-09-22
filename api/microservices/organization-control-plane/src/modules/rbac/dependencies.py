"""RBAC route enforcement (ADR-029/AD-01) — the first place the dormant `rbac.service.can()` seam
is wired into a route. `require_permission(slug)` gates a mutating route on the authenticated
org-user holding a permission from the Admin CP master catalog.

Permission slug->id resolution mirrors the `apikey.manage` idiom (modules/api_keys/dependencies):
the id is owned by Admin CP (ADR-019), resolved from `GET /internal/v1/permissions` and cached on
`app.state.permission_ids`; a one-shot lazy refresh self-heals a late-starting Admin CP. Failure is
fail-closed: an unresolved id denies (403), never opens. The actual outbound call lives in
`rbac.service.fetch_permission_id_by_slug` — no httpx call site in this module (router →
dependencies/service/errors, ADR-003 convention).
"""

from __future__ import annotations

import uuid

from fastapi import Depends, Request

from errors import ForbiddenError
from modules.auth.dependencies import get_current_user
from modules.rbac.service import can, fetch_permission_id_by_slug
from modules.users.models import User


async def resolve_permission_id(request: Request, slug: str) -> uuid.UUID | None:
    """Cached slug->id lookup on app.state; one catalog fetch, lazy one-shot refresh on a miss."""
    cache = getattr(request.app.state, "permission_ids", None)
    if cache is None:
        cache = {}
        request.app.state.permission_ids = cache
    if cache.get(slug) is None:  # unresolved yet, or a prior fail — try (again) once
        cache[slug] = await fetch_permission_id_by_slug(request.app.state.admin_client, slug)
    return cache[slug]


def require_permission(slug: str):
    """A FastAPI dependency that authorizes the current org user against `slug`. Reuses
    get_current_user (enforces the `org` claim + the must_change_password forced-rotation gate before
    any RBAC work). Fails closed: an unresolved permission id, or a caller lacking the permission in
    their effective set, is a 403. Returns the authenticated `User` on success."""

    async def _dep(request: Request, actor: User = Depends(get_current_user)) -> User:
        perm_id = await resolve_permission_id(request, slug)
        if perm_id is None:
            raise ForbiddenError()  # fail-closed: unresolved permission ⇒ deny
        async with request.app.state.sessionmaker() as session:
            if not await can(session, actor.organization_id, actor.id, perm_id):
                raise ForbiddenError()
        return actor

    return _dep
