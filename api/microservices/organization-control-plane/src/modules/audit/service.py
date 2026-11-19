"""Audit recording + querying. `record` adds a row to the caller's session so it commits (or rolls
back) atomically with the change it describes; `record_detached` opens its own short transaction for
the routes that own their sessions. The detached form must never break the audited request (the
action has already committed), so it swallows and logs its own failures.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime

from fastapi import Request
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modules.auth.jwt import decode_access_token
from modules.users.models import User

from .models import AuditLog

logger = logging.getLogger(__name__)

ACTOR_ID_HEADER = "X-Actor-Id"
ACTOR_EMAIL_HEADER = "X-Actor-Email"


@dataclass(frozen=True)
class Actor:
    type: str  # user | admin | service | anonymous
    id: str | None = None
    email: str | None = None

    @classmethod
    def from_user(cls, user: User) -> Actor:
        return cls("user", str(user.id), user.email)


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
    """Best-effort identity of the caller of a /v1 route (service bearer OR org-user JWT).

    A service-key caller is Admin CP / the portal backend; Admin CP forwards the signed-in
    super-admin in X-Actor-* headers, which are trusted ONLY under the service bearer (never for a
    JWT caller, whose identity comes from the verified token)."""
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return Actor("anonymous")
    settings = request.app.state.settings
    if settings.service_api_key and secrets.compare_digest(token, settings.service_api_key):
        email = request.headers.get(ACTOR_EMAIL_HEADER)
        if email:
            return Actor("admin", request.headers.get(ACTOR_ID_HEADER), email)
        return Actor("service")
    try:
        claims = decode_access_token(token, secret=settings.jwt_secret)
    except Exception:
        return Actor("anonymous")
    return Actor("user", str(claims.get("sub")), claims.get("email"))


def ctx(request: Request, actor: Actor | None = None) -> dict:
    """The `actor` + `ip` kwargs every record call shares, resolved from the request."""
    return {"actor": actor or actor_from_request(request), "ip": client_ip(request)}


def _row(
    *,
    organization_id: uuid.UUID | None,
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


async def org_id_for_email(
    sessionmaker: async_sessionmaker[AsyncSession], email: str
) -> uuid.UUID | None:
    async with sessionmaker() as session:
        return await session.scalar(
            select(User.organization_id).where(func.lower(User.email) == email.lower()).limit(1)
        )


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def list_logs(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    search: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[AuditLog], int]:
    """Newest-first page + total. `organization_id=None` means ALL orgs — only the internal
    (service-key) route may pass None; the end-user route always passes the caller's own org."""
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
        .limit(limit)
        .offset(offset)
    )
    return list(rows), int(total or 0)
