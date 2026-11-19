"""Invitations business logic. An invitation is created by an authorised caller (an org admin through
the Org Portal, or a super-admin through Admin CP) and accepted by the invitee through a public,
unauthenticated page: they follow the one-time link and choose their own password, which creates the
user. The raw token is shown once; only its SHA-256 is stored. Services flush() but never commit()
(get_session owns the transaction)."""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from errors import ConflictError, GoneError, NotFoundError, UnprocessableError
from modules.auth.password import hash_password, validate_password_policy
from modules.organizations.models import Organization
from modules.roles.models import Role, UserRole
from modules.users.models import User
from modules.users.service import _flush_unique

from .models import Invitation

DEFAULT_ROLE = "org_user"
_INVALID = "This invitation link is not valid."
_USED = "This invitation has already been used or replaced. Ask for a new link."
_EXPIRED = "This invitation has expired. Ask for a new link."


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def invite_path(raw_token: str) -> str:
    return f"/accept-invite?token={raw_token}"


async def _resolve_role(
    session: AsyncSession, org_id: uuid.UUID, role_id: uuid.UUID | None, role_name: str | None
) -> Role:
    """The role must belong to THIS org -- a role id from another tenant is a 422, never accepted."""
    if role_id is not None:
        role = await session.scalar(
            select(Role).where(Role.id == role_id, Role.organization_id == org_id)
        )
        if role is None:
            raise UnprocessableError(
                "That role does not exist in this organization", field="role_id"
            )
        return role
    name = role_name or DEFAULT_ROLE
    role = await session.scalar(
        select(Role).where(Role.organization_id == org_id, Role.name == name)
    )
    if role is None:
        raise UnprocessableError(
            f"Role '{name}' does not exist in this organization", field="role_name"
        )
    return role


async def create_invitation(
    session: AsyncSession,
    settings,
    org_id: uuid.UUID,
    *,
    email: str,
    name: str | None,
    role_id: uuid.UUID | None,
    role_name: str | None,
    invited_by: str | None,
) -> tuple[Invitation, Organization, Role, str]:
    """Issue an invitation. Returns (invitation, org, role, raw_token) -- the raw token exists only here."""
    org = await session.get(Organization, org_id)
    if org is None:
        raise UnprocessableError(f"Organization {org_id} does not exist")
    role = await _resolve_role(session, org_id, role_id, role_name)

    # Local-auth email is globally unique (migration 0005), and an org directory row with this email
    # would collide on accept. Refuse up front so the inviter hears about it now, not the invitee later.
    existing = await session.scalar(select(User).where(func.lower(User.email) == email))
    if existing is not None:
        raise ConflictError("A user with this email already exists", field="email")

    # One live invitation per (org, email): issuing a new link replaces any earlier pending one.
    await session.execute(
        update(Invitation)
        .where(
            Invitation.organization_id == org_id,
            Invitation.email == email,
            Invitation.status == "pending",
        )
        .values(status="revoked")
    )

    raw = secrets.token_urlsafe(32)
    inv = Invitation(
        organization_id=org_id,
        email=email,
        name=name,
        role_id=role.id,
        token_hash=hash_token(raw),
        status="pending",
        invited_by=invited_by,
        expires_at=datetime.now(UTC) + timedelta(hours=settings.invitation_ttl_hours),
    )
    session.add(inv)
    await session.flush()
    return inv, org, role, raw


async def _load_pending(session: AsyncSession, raw_token: str) -> Invitation:
    inv = await session.scalar(
        select(Invitation).where(Invitation.token_hash == hash_token(raw_token))
    )
    if inv is None:
        raise NotFoundError(_INVALID)
    if inv.status != "pending":
        raise GoneError(_USED)
    if _aware(inv.expires_at) <= datetime.now(UTC):
        raise GoneError(_EXPIRED)
    return inv


async def inspect_invitation(
    session: AsyncSession, raw_token: str
) -> tuple[Invitation, Organization, Role]:
    inv = await _load_pending(session, raw_token)
    org = await session.get(Organization, inv.organization_id)
    role = await session.get(Role, inv.role_id)
    if org is None or role is None:
        raise GoneError("This invitation is no longer available. Ask for a new link.")
    return inv, org, role


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def list_invitations(
    session: AsyncSession,
    org_id: uuid.UUID | None,
    *,
    search: str | None = None,
    role_name: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> tuple[list[tuple[Invitation, str]], int]:
    """Pending invitations, newest first, with their role's name. `org_id=None` means ALL orgs -- only the
    internal (service-key) route may pass None; the end-user route always passes the caller's own org.
    Expired-but-unaccepted invitations stay listed (flagged by their `expires_at`) so they can be revoked.
    """
    conds = [Invitation.status == "pending"]
    if org_id is not None:
        conds.append(Invitation.organization_id == org_id)
    if search:
        conds.append(Invitation.email.ilike(_like(search.strip().lower()), escape="\\"))
    if role_name:
        conds.append(Role.name == role_name)
    joined = select(Invitation, Role.name).join(Role, Role.id == Invitation.role_id).where(*conds)
    total = await session.scalar(
        select(func.count())
        .select_from(Invitation)
        .join(Role, Role.id == Invitation.role_id)
        .where(*conds)
    )
    rows = await session.execute(
        joined.order_by(Invitation.created_at.desc(), Invitation.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return [(inv, name) for inv, name in rows.all()], int(total or 0)


async def revoke_invitation(
    session: AsyncSession, org_id: uuid.UUID | None, invitation_id: uuid.UUID
) -> Invitation:
    """Cancel a pending invitation so its link stops working. Scoped to `org_id` when given, so another
    tenant's invitation id is a 404, never revoked."""
    q = select(Invitation).where(Invitation.id == invitation_id)
    if org_id is not None:
        q = q.where(Invitation.organization_id == org_id)
    inv = await session.scalar(q)
    if inv is None:
        raise NotFoundError("Invitation not found")
    if inv.status != "pending":
        raise ConflictError(f"This invitation was already {inv.status}")
    inv.status = "revoked"
    await session.flush()
    return inv


def _username_base(email: str) -> str:
    local = email.split("@", 1)[0].lower()
    return re.sub(r"[^a-z0-9._-]", "", local) or "user"


async def _free_username(session: AsyncSession, org_id: uuid.UUID, email: str) -> str:
    base = _username_base(email)
    taken = set(
        await session.scalars(
            select(User.username).where(
                User.organization_id == org_id, User.username.like(f"{base}%")
            )
        )
    )
    if base not in taken:
        return base
    n = 2
    while f"{base}{n}" in taken:
        n += 1
    return f"{base}{n}"


async def accept_invitation(
    session: AsyncSession, raw_token: str, password: str
) -> tuple[User, Invitation]:
    """Create the invited user with their own chosen password and the invited role, and spend the token."""
    inv, _org, role = await inspect_invitation(session, raw_token)
    validate_password_policy(password)  # 422 before any hash work or write
    user = await _spend_and_create_user(session, inv, role, hash_password(password))
    return user, inv


async def _spend_and_create_user(
    session: AsyncSession, inv: Invitation, role: Role, password_hash: str | None
) -> User:
    # Spend the invitation atomically first: of two concurrent accepts only one sees rowcount 1.
    spent = await session.execute(
        update(Invitation)
        .where(Invitation.id == inv.id, Invitation.status == "pending")
        .values(status="accepted", accepted_at=datetime.now(UTC))
    )
    if spent.rowcount != 1:
        raise GoneError(_USED)

    name = (inv.name or "").strip()
    first, _, last = name.partition(" ")
    username = await _free_username(session, inv.organization_id, inv.email)
    user = User(
        organization_id=inv.organization_id,
        username=username,
        email=inv.email,
        first_name=first or None,
        last_name=last.strip() or None,
        display_name=name or None,
        meta={},
        password_hash=password_hash,
        # They just chose this password themselves, so there is nothing to force them to rotate.
        must_change_password=False,
    )
    session.add(user)
    await _flush_unique(
        session, inv.organization_id, inv.email, username, has_password=password_hash is not None
    )
    session.add(UserRole(user_id=user.id, role_id=role.id))
    await session.flush()
    return user
