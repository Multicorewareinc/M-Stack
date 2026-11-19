"""Audit recording + the cross-control-plane read. `record` adds a row to the caller's session so it
commits (or rolls back) atomically with the change it describes; `record_detached` opens its own
short transaction for routes/workflows that own their sessions and must never fail the audited
request (the action has already committed), so it swallows and logs its own failures.

`list_logs` merges this service's own (platform) events with Org CP's (users, roles, API keys, org
auth) so a super-admin sees one chronological feed across every organization.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from fastapi import Request
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from errors import UpstreamError
from modules.auth.jwt import decode_access_token

from .models import AuditLog
from .schemas import AuditLogOut, AuditLogPage

logger = logging.getLogger(__name__)

# Org CP caps a single internal page; the merge needs (offset + limit) rows from each side.
MAX_WINDOW = 5000


@dataclass(frozen=True)
class Actor:
    type: str  # admin | service | anonymous
    id: str | None = None
    email: str | None = None


def client_ip(request: Request) -> str | None:
    settings = request.app.state.settings
    peer = request.client.host if request.client else None
    if settings.trusted_proxy_count > 0:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            parts = [p.strip() for p in xff.split(",") if p.strip()]
            if parts:
                idx = len(parts) - settings.trusted_proxy_count
                return parts[max(0, min(idx, len(parts) - 1))]
    return peer


def actor_from_request(request: Request) -> Actor:
    """Identity of a /v1 caller: a super-admin's verified JWT, or the static admin key."""
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return Actor("anonymous")
    settings = request.app.state.settings
    if settings.admin_api_key and secrets.compare_digest(token, settings.admin_api_key):
        return Actor("service")
    try:
        claims = decode_access_token(token, secret=settings.jwt_secret)
    except Exception:
        return Actor("anonymous")
    return Actor("admin", str(claims.get("sub")), claims.get("email"))


def ctx(request: Request, actor: Actor | None = None) -> dict:
    """The `actor` + `ip` kwargs every record call shares, resolved from the request."""
    return {"actor": actor or actor_from_request(request), "ip": client_ip(request)}


def _row(
    *,
    organization_id: uuid.UUID | None = None,
    actor: Actor,
    action: str,
    resource_type: str,
    resource_id: object = None,
    resource_name: str | None = None,
    details: dict | None = None,
    ip: str | None = None,
) -> AuditLog:
    return AuditLog(
        organization_id=organization_id,
        actor_type=actor.type,
        actor_id=actor.id,
        actor_email=actor.email,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        resource_name=resource_name,
        details=details or {},
        ip_address=ip,
    )


async def record(session: AsyncSession, **kwargs) -> None:
    """Add an audit row to `session` (committed with the caller's own transaction)."""
    session.add(_row(**kwargs))
    await session.flush()


async def record_detached(sessionmaker: async_sessionmaker[AsyncSession], **kwargs) -> None:
    """Persist an audit row in its own transaction. A failure is logged, never raised."""
    try:
        async with sessionmaker() as session:
            session.add(_row(**kwargs))
            await session.commit()
    except Exception:  # pragma: no cover - defensive
        logger.exception("failed to write audit log for %s", kwargs.get("action"))


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def _local_page(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID | None,
    action: str | None,
    resource_type: str | None,
    search: str | None,
    since: datetime | None,
    until: datetime | None,
    window: int,
) -> tuple[list[AuditLog], int]:
    conds = []
    if organization_id is not None:
        conds.append(AuditLog.organization_id == organization_id)
    if action:
        conds.append(AuditLog.action == action)
    if resource_type:
        conds.append(AuditLog.resource_type == resource_type)
    if since is not None:
        conds.append(AuditLog.created_at >= since)
    if until is not None:
        conds.append(AuditLog.created_at <= until)
    if search:
        pat = _like(search)
        conds.append(
            or_(
                AuditLog.actor_email.ilike(pat, escape="\\"),
                AuditLog.resource_name.ilike(pat, escape="\\"),
                AuditLog.action.ilike(pat, escape="\\"),
            )
        )
    total = await session.scalar(select(func.count()).select_from(AuditLog).where(*conds))
    rows = await session.scalars(
        select(AuditLog)
        .where(*conds)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(window)
    )
    return list(rows), int(total or 0)


async def _org_page(
    org_client: httpx.AsyncClient, params: dict, window: int
) -> tuple[list[dict], int]:
    try:
        resp = await org_client.get(
            "/internal/v1/audit-logs", params={**params, "limit": window, "offset": 0}
        )
    except httpx.HTTPError as exc:
        raise UpstreamError("Org CP unavailable") from exc
    if resp.status_code != 200:
        raise UpstreamError(f"Org CP returned {resp.status_code} listing audit logs")
    body = resp.json()
    return body["items"], int(body["total"])


def _sort_key(item: AuditLogOut) -> tuple[datetime, str]:
    ts = item.created_at if item.created_at.tzinfo else item.created_at.replace(tzinfo=UTC)
    return ts, str(item.id)


async def list_logs(
    session: AsyncSession,
    org_client: httpx.AsyncClient,
    *,
    organization_id: uuid.UUID | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    search: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> AuditLogPage:
    """One newest-first page across platform + organization events, all orgs (or one, when
    `organization_id` is given). Each side is asked for its newest `offset + limit` rows, which are
    merged and sliced — exact for any page within MAX_WINDOW."""
    window = offset + limit
    local, local_total = await _local_page(
        session,
        organization_id=organization_id,
        action=action,
        resource_type=resource_type,
        search=search,
        since=since,
        until=until,
        window=window,
    )
    params = {
        k: v.isoformat() if isinstance(v, datetime) else str(v)
        for k, v in {
            "organization_id": organization_id,
            "action": action,
            "resource_type": resource_type,
            "search": search,
            "since": since,
            "until": until,
        }.items()
        if v is not None
    }
    remote, remote_total = await _org_page(org_client, params, window)

    items = [AuditLogOut.model_validate(r) for r in local]
    items += [AuditLogOut.model_validate({**r, "source": "organization"}) for r in remote]
    items.sort(key=_sort_key, reverse=True)
    return AuditLogPage(
        items=items[offset : offset + limit],
        total=local_total + remote_total,
        limit=limit,
        offset=offset,
    )
