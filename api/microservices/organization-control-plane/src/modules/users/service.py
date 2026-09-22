"""Users business logic — tenant-scoped. Every function takes org_id and filters on it; there is
NO unscoped query for a tenant operation (§45). Cross-org access resolves to 404 (§46, D2). All
DB access here; services flush() but never commit() (D3)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from errors import ConflictError, NotFoundError, UnprocessableError
from modules.api_keys import cache
from modules.api_keys.models import ApiKey
from modules.auth.models import RefreshToken
from modules.auth.password import hash_password, validate_password_policy
from modules.organizations.models import Organization
from modules.roles.models import Role, UserRole

from .models import User
from .schemas import UserCreate, UserUpdate


async def _require_org(session: AsyncSession, org_id: uuid.UUID) -> None:
    """422 when the tenant does not exist (app-level; FK is the Postgres backstop, D4)."""
    if await session.get(Organization, org_id) is None:
        raise UnprocessableError(f"Organization {org_id} does not exist")


async def _flush_unique(
    session: AsyncSession,
    org_id: uuid.UUID,
    email: str,
    username: str,
    *,
    has_password: bool = False,
    exclude_user_id: uuid.UUID | None = None,
) -> None:
    try:
        await session.flush()
    except IntegrityError as exc:
        # The DB constraint is the source of truth (no pre-check SELECT, race-free); once it
        # fires, roll back (a session with a failed flush can't be queried again otherwise) and
        # do a targeted post-failure lookup so the error can point the form at the field that
        # actually collided, rather than blaming both indiscriminately. On an UPDATE, rollback
        # reverts this same row to its pre-update state, so `exclude_user_id` excludes it from
        # these lookups — otherwise an update that collides on email/password could spuriously
        # "find itself" via an unrelated, unchanged username and misreport the wrong field.
        await session.rollback()
        email_q = select(User).where(User.organization_id == org_id, User.email == email)
        username_q = select(User).where(User.organization_id == org_id, User.username == username)
        if exclude_user_id is not None:
            email_q = email_q.where(User.id != exclude_user_id)
            username_q = username_q.where(User.id != exclude_user_id)
        if await session.scalar(email_q) is not None:
            raise ConflictError(f"User email '{email}' already exists in this organization", field="email") from exc
        if await session.scalar(username_q) is not None:
            raise ConflictError(
                f"Username '{username}' already exists in this organization", field="username"
            ) from exc
        if has_password:
            # Neither per-org constraint fired — this must be the cross-org local-auth-email index
            # (migration 0005): another org already has a local-auth-capable user with this email.
            raise ConflictError(
                f"Email '{email}' already has local-auth credentials in a different organization",
                field="password",
            ) from exc
        raise  # defensive: an unrelated DB error, not one of the constraints above


async def create_user(session: AsyncSession, org_id: uuid.UUID, data: UserCreate) -> User:
    await _require_org(session, org_id)
    user = User(
        organization_id=org_id,
        username=data.username,
        email=data.email,
        first_name=data.first_name,
        last_name=data.last_name,
        display_name=data.display_name,
        meta=data.metadata,
    )
    has_password = data.password is not None
    if has_password:
        # Provisioning follow-up (ADR-025 SP-01 design open question): an admin sets the user's
        # initial password at creation. validate_password_policy raises WeakPasswordError (422)
        # before any hash work — same policy the /api/auth surface enforces. Forces rotation on
        # first login (mirrors seed_admin's must_change_password=True).
        validate_password_policy(data.password)
        user.password_hash = hash_password(data.password)
        user.must_change_password = True
    session.add(user)
    await _flush_unique(session, org_id, data.email, data.username, has_password=has_password)
    return user


async def assign_default_role(
    session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID, role_name: str
) -> None:
    """Assign an org's default role (by name) to a freshly created user (ADR-029/AD-05). Best-effort:
    if the role is absent (an org provisioned before default-role seeding, SP-01), this is a no-op —
    creation must not hard-fail on a missing default role; the user can be assigned roles later.
    ponytail: a direct name lookup + insert, not the full set_user_roles replace machinery."""
    role = await session.scalar(
        select(Role).where(Role.organization_id == org_id, Role.name == role_name)
    )
    if role is None:  # ponytail: pre-seeding org — assignable later via PUT /v1/users/{id}/roles
        return
    session.add(UserRole(user_id=user_id, role_id=role.id))
    await session.flush()


async def list_users(session: AsyncSession, org_id: uuid.UUID) -> list[User]:
    # ponytail: unpaginated; add limit/offset when a tenant's user count grows past a screenful.
    result = await session.scalars(
        select(User).where(User.organization_id == org_id).order_by(User.created_at)
    )
    return list(result)


async def get_user(session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID) -> User:
    """Scoped on BOTH ids — a user id from another org resolves to 404 (cross-org protection)."""
    user = await session.scalar(
        select(User).where(User.id == user_id, User.organization_id == org_id)
    )
    if user is None:
        raise NotFoundError(f"User {user_id} not found")
    return user


async def update_user(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    data: UserUpdate,
) -> User:
    """Edit a user. Owns its own transaction so that, when the update deactivates the user (status →
    non-active), the user's API keys can be evicted from the verify cache post-commit — otherwise a
    cached record would keep the gateway admitting them until TTL (ADR-026). Commit-then-evict,
    best-effort. Cross-org/missing → 404 (unchanged)."""
    async with sessionmaker() as session:
        user = await get_user(session, org_id, user_id)
        fields = data.model_dump(exclude_unset=True)
        if "metadata" in fields:
            user.meta = fields.pop("metadata")
        if "password" in fields:
            # Admin-initiated (re)set — air-gapped, no self-service email reset (ADR-008 §5). Same
            # policy + forced-rotation semantic as create_user; the raw password is never persisted.
            raw = fields.pop("password")
            validate_password_policy(raw)
            user.password_hash = hash_password(raw)
            user.must_change_password = True
        for key, value in fields.items():
            setattr(user, key, value)
        # Reflects the row's post-update state (not just whether THIS call set a password) — e.g.
        # changing `email` alone on an already-password-having row can collide too.
        await _flush_unique(
            session,
            org_id,
            user.email,
            user.username,
            has_password=user.password_hash is not None,
            exclude_user_id=user_id,
        )
        # A deactivation must reach key verification promptly: collect this user's live key hashes so
        # they can be evicted after commit (the gateway re-reads owner_active on the next miss). It
        # must also kill the user's own session: revoke every live refresh token in the SAME
        # transaction, so a suspended user cannot refresh their way back in (the active-check on
        # get_current_user/refresh_session is the other half of this — this is the trigger).
        deactivating = "status" in fields and fields["status"] != "active"
        hashes: list[str] = []
        if deactivating:
            hashes = list(
                await session.scalars(
                    select(ApiKey.key_hash).where(
                        ApiKey.owner_id == user_id,
                        ApiKey.org_id == org_id,
                        ApiKey.revoked_at.is_(None),
                    )
                )
            )
            await session.execute(
                sa_update(RefreshToken)
                .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
                .values(revoked_at=datetime.now(UTC))
            )
        await session.commit()
    for key_hash in hashes:  # commit-then-evict, best-effort
        await cache.evict(settings, key_hash)
    return user


async def delete_user(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    """Delete a user and, in the SAME transaction, revoke that user's org-scoped API keys — so a
    removed member's keys stop verifying with no window (reference §10). Owns its own session so the
    cache eviction is genuinely post-commit (commit-then-evict, best-effort). Cross-org/missing → 404."""
    async with sessionmaker() as session:
        user = await get_user(session, org_id, user_id)  # 404 if cross-org/missing (unchanged)
        keys = await session.scalars(
            select(ApiKey).where(
                ApiKey.owner_id == user_id,
                ApiKey.org_id == org_id,
                ApiKey.revoked_at.is_(None),
            )
        )
        now = datetime.now(UTC)
        hashes: list[str] = []
        for key in keys:
            key.revoked_at = now
            hashes.append(key.key_hash)
        await session.delete(user)
        await session.commit()
    for key_hash in hashes:  # commit-then-evict, best-effort (a Valkey outage never fails the delete)
        await cache.evict(settings, key_hash)


async def count_active_users_total(session: AsyncSession) -> int:
    """Platform-wide count for Admin CP's dashboard (§184) — one query, not a sum of per-org
    fetches."""
    return await session.scalar(select(func.count()).select_from(User).where(User.status == "active")) or 0


async def count_users_by_org(session: AsyncSession, org_ids: list[uuid.UUID]) -> dict[str, int]:
    """One grouped query for every requested org — the bulk counterpart to list_users, so Admin
    CP's organizations list never fans out a per-org request (no N+1, §184). Orgs with zero users
    are simply absent from the result; the caller defaults those to 0."""
    if not org_ids:
        return {}
    rows = await session.execute(
        select(User.organization_id, func.count(User.id))
        .where(User.organization_id.in_(org_ids))
        .group_by(User.organization_id)
    )
    return {str(org_id): count for org_id, count in rows.all()}


async def list_all_users(session: AsyncSession) -> list[User]:
    """Platform-wide, unscoped by org — for Admin CP's own user-directory proxy (Super-Admin
    only; the tenant-scoping rule (§45) is specifically about org-owned /v1 operations, not this
    internal, cross-tenant surface Admin CP alone may call). ponytail: unpaginated, matches every
    other list_* convention in this codebase; add limit/offset when the platform's total user
    count grows past a screenful."""
    result = await session.scalars(select(User).order_by(User.created_at))
    return list(result)


async def get_user_by_id(session: AsyncSession, user_id: uuid.UUID) -> User:
    """Unscoped lookup by id alone — the one place a user is resolved without already knowing
    its org (Admin CP's flat user-detail proxy learns organization_id from this, then uses the
    normal per-org routes for anything further)."""
    user = await session.get(User, user_id)
    if user is None:
        raise NotFoundError(f"User {user_id} not found")
    return user
