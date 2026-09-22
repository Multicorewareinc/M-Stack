"""Auth orchestration — login, session issuance, refresh rotation, logout, change-password.

Raises domain errors only (never HTTPException). Each method owns its OWN transaction (design D6):
the request-scoped get_session dependency commits once and rolls back on any exception, so it cannot
back the auth flows that must persist-then-raise (refresh reuse-detection family-revoke). Every
method here opens `async with sessionmaker() as session`, does its work, and commits itself; the
reuse-detection branch commits the family-revoke BEFORE raising SessionExpiredError.

Async adaptation of the sync air-gapped reference: AsyncSession + await; no Membership table (org is
resolved from users.organization_id); PyJWT HS256; Argon2id via modules/auth/password.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from errors import (
    InvalidCredentialsError,
    PasswordUnchangedError,
    SessionExpiredError,
    UserDeactivatedError,
)
from modules.organizations.models import Organization
from modules.rbac import service as rbac_service
from modules.users.models import User

from . import lockout
from .jwt import encode_access_token
from .models import RefreshToken
from .password import (
    dummy_verify,
    hash_password,
    validate_password_policy,
    verify_password,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime) -> datetime:
    """Normalise a stored timestamp to tz-aware UTC (aiosqlite may return it naive)."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _hash_refresh(raw: str) -> str:
    """SHA-256 hex of a raw refresh token — what the refresh_tokens row stores (never the raw)."""
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


@dataclass
class IssuedSession:
    """Plain result of establishing a platform session — the router turns it into a LoginResponse
    body + the HttpOnly refresh cookie. The raw refresh token is returned exactly once."""

    access_token: str
    expires_in: int
    refresh_token: str
    refresh_max_age: int
    org: Organization | None
    permissions: list[str]


@dataclass
class LoginOutcome:
    issued: IssuedSession
    user: User
    must_change_password: bool


async def resolve_user_org(session: AsyncSession, user: User) -> Organization | None:
    """Resolve the caller's organization from users.organization_id, server-side (no Membership
    table — D3). A pending/rejected organization confers no usable org context, so it resolves to
    None (the caller receives org == null and no `org` JWT claim → ORG_REQUIRED on org routes)."""
    if user.organization_id is None:
        return None
    org = await session.get(Organization, user.organization_id)
    if org is None or org.status in ("pending", "rejected"):
        return None
    return org


async def _resolve_permissions(
    session: AsyncSession, org: Organization | None, user: User
) -> list[str]:
    """Reuse the existing rbac effective-permission service (D3b — no new resolution logic).

    ponytail: no local permission-slug store exists (the permission catalog is Admin CP-owned and
    proxied, never persisted here), so the effective permission ids are returned stringified.
    Upgrade path = join to a slug once a local catalog projection lands.
    """
    if org is None:
        return []
    ids = await rbac_service.effective_permission_ids(session, org.id, user.id)
    return [str(i) for i in ids]


async def _issue(session: AsyncSession, user: User, settings) -> IssuedSession:
    """Mint the access token + a rotating refresh token, persist only the refresh hash, and resolve
    org + permissions. Encoding runs BEFORE the refresh row is added, so an unset signing secret
    (→ 500) leaves nothing persisted and issues no token."""
    org = await resolve_user_org(session, user)
    org_claim = str(user.organization_id) if org is not None else None
    access_token = encode_access_token(
        sub=str(user.id),
        email=user.email,
        org=org_claim,
        secret=settings.jwt_secret,
        ttl_seconds=settings.jwt_access_ttl_seconds,
    )
    raw_refresh = secrets.token_urlsafe(32)
    session.add(
        RefreshToken(
            user_id=user.id,
            token_hash=_hash_refresh(raw_refresh),
            expires_at=_now() + timedelta(seconds=settings.jwt_refresh_ttl_seconds),
        )
    )
    permissions = await _resolve_permissions(session, org, user)
    return IssuedSession(
        access_token=access_token,
        expires_in=settings.jwt_access_ttl_seconds,
        refresh_token=raw_refresh,
        refresh_max_age=settings.jwt_refresh_ttl_seconds,
        org=org,
        permissions=permissions,
    )


async def login_local(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings,
    email: str,
    password: str,
    *,
    ip: str = "unknown",
) -> LoginOutcome:
    """Validate local credentials and issue a session, in the fixed gate order (D4).

    (1) local_auth_enabled — before any DB/redis/hash. (2) lockout check_locked — before the lookup.
    (3) lookup by lower(email) — a DB error propagates as 500, NEVER remapped to 401. (4) uniform
    credential verify — unknown-email / no-hash / wrong-password all do equivalent Argon2id work and
    raise the identical InvalidCredentialsError, funneled through one except that records the lockout
    failure. (5) deactivation gate — only AFTER a correct password. (6) issue session — clear
    lockout, stamp last_login_at, mint tokens, resolve org.
    """
    from errors import LocalAuthDisabledError

    # (1) Gate before any DB/redis/hash work.
    if not settings.local_auth_enabled:
        raise LocalAuthDisabledError()

    # (2) Lockout check — before the lookup, so a locked attempt costs no DB/hash work.
    await lockout.check_locked(email, ip, settings)

    async with sessionmaker() as session:
        # (3) Lookup by lowercased email. A transient DB failure fails closed as 500 (propagates) —
        # never remapped to 401 (which would falsely assert a credential decision).
        normalized = email.lower()
        try:
            user = await session.scalar(
                select(User).where(func.lower(User.email) == normalized)
            )
        except OperationalError:
            raise

        # (4) Uniform credential-failure branches — one Argon2 verify each, identical error; the
        # single except records the lockout failure.
        try:
            if user is None:
                dummy_verify()
                raise InvalidCredentialsError()
            if user.password_hash is None:
                dummy_verify()
                raise InvalidCredentialsError()
            if not verify_password(password, user.password_hash):
                raise InvalidCredentialsError()
        except InvalidCredentialsError:
            await lockout.record_failure(email, ip, settings)
            raise

        # (5) Deactivation gate — after verify, so a wrong password against a suspended account is
        # indistinguishable from any other wrong password (never USER_DEACTIVATED).
        if not user.active:
            raise UserDeactivatedError()

        # (6) Success: clear lockout, stamp last_login_at, issue the session, commit.
        await lockout.clear(email, ip, settings)
        user.last_login_at = _now()
        issued = await _issue(session, user, settings)
        must_change = user.must_change_password
        await session.commit()
        return LoginOutcome(issued=issued, user=user, must_change_password=must_change)


async def refresh_session(
    sessionmaker: async_sessionmaker[AsyncSession], settings, raw_refresh: str | None
) -> LoginOutcome:
    """Validate + atomically rotate a refresh token (D5).

    Missing / unknown / expired → SessionExpiredError. A presented already-revoked token is a
    reuse (stolen-cookie) signal: revoke the whole family, COMMIT that revoke, then raise
    SessionExpiredError. Otherwise rotate via a single conditional UPDATE ... WHERE revoked_at IS
    NULL, so a concurrent loser updates 0 rows and mints nothing.
    """
    if not raw_refresh:
        raise SessionExpiredError()

    token_hash = _hash_refresh(raw_refresh)
    async with sessionmaker() as session:
        row = await session.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )
        if row is None:
            raise SessionExpiredError()

        # Revoked-but-reused → family-revoke, commit BEFORE raising (persist-then-raise, D6).
        if row.revoked_at is not None:
            await session.execute(
                update(RefreshToken)
                .where(RefreshToken.user_id == row.user_id, RefreshToken.revoked_at.is_(None))
                .values(revoked_at=_now())
            )
            await session.commit()
            raise SessionExpiredError()

        if _aware(row.expires_at) <= _now():
            raise SessionExpiredError()

        # Atomic rotation: only the caller whose conditional update affects the row proceeds.
        result = await session.execute(
            update(RefreshToken)
            .where(RefreshToken.id == row.id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=_now())
        )
        if result.rowcount == 0:
            raise SessionExpiredError()

        user = await session.get(User, row.user_id)
        if user is None:
            raise SessionExpiredError()

        # Active gate: a suspended/deactivated user must not be able to mint a fresh access token
        # by refreshing — the rotation above already burned the presented token, so revoke the rest
        # of the family too and treat this exactly like an expired session (no distinct signal to a
        # cookie holder who is no longer the account owner's session).
        if not user.active:
            await session.execute(
                update(RefreshToken)
                .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
                .values(revoked_at=_now())
            )
            await session.commit()
            raise SessionExpiredError()

        issued = await _issue(session, user, settings)
        must_change = user.must_change_password
        await session.commit()
        return LoginOutcome(issued=issued, user=user, must_change_password=must_change)


async def logout_session(
    sessionmaker: async_sessionmaker[AsyncSession], raw_refresh: str | None
) -> None:
    """Revoke ONLY the presented device's refresh token (D5). Idempotent: a missing / already-revoked
    token is a no-op (updates 0 rows)."""
    if not raw_refresh:
        return
    async with sessionmaker() as session:
        await session.execute(
            update(RefreshToken)
            .where(
                RefreshToken.token_hash == _hash_refresh(raw_refresh),
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=_now())
        )
        await session.commit()


async def change_password(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings,
    user_id: uuid.UUID,
    current_password: str,
    new_password: str,
) -> None:
    """Change the authenticated user's password (D5 order).

    Verify current (uniform 401 on a miss, dummy_verify for a no-hash account) → policy check →
    reject a no-op (PASSWORD_UNCHANGED) → re-hash + clear must_change_password + revoke ALL the
    user's refresh tokens, in one transaction.
    """
    async with sessionmaker() as session:
        user = await session.get(User, user_id)
        if user is None:
            raise InvalidCredentialsError()

        # 1. Verify the current password — same hashing work on every miss; uniform 401.
        if not user.password_hash:
            dummy_verify()
            raise InvalidCredentialsError()
        if not verify_password(current_password, user.password_hash):
            raise InvalidCredentialsError()

        # 2. Policy (WeakPasswordError / 422). 3. Reject no-op (only after current verified).
        validate_password_policy(new_password)
        if verify_password(new_password, user.password_hash):
            raise PasswordUnchangedError()

        # 4. One transaction: re-hash, clear the flag, revoke every refresh token.
        user.password_hash = hash_password(new_password)
        user.must_change_password = False
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=_now())
        )
        await session.commit()
