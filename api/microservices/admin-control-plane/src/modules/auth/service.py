"""Auth orchestration — login, session issuance, refresh rotation, logout, change-password.

Org-less super-admin variant (SP-02): no organization, no permissions, no RBAC — just the
super-admin identity. Raises domain errors only (never HTTPException). Each method owns its OWN
transaction (design D6): the request-scoped get_session dependency commits once and rolls back on
any exception, so it cannot back the auth flows that must persist-then-raise (refresh
reuse-detection family-revoke). Every method here opens `async with sessionmaker() as session`, does
its work, and commits itself; the reuse-detection branch commits the family-revoke BEFORE raising
SessionExpiredError.
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

from . import lockout
from .jwt import encode_access_token
from .models import AdminUser, RefreshToken
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
    """Plain result of establishing a super-admin session — the router turns it into a
    LoginResponse body + the HttpOnly refresh cookie. The raw refresh token is returned exactly
    once. No org, no permissions (super-admins are org-less)."""

    access_token: str
    expires_in: int
    refresh_token: str
    refresh_max_age: int


@dataclass
class LoginOutcome:
    issued: IssuedSession
    user: AdminUser
    must_change_password: bool


async def _issue(session: AsyncSession, user: AdminUser, settings) -> IssuedSession:
    """Mint the access token + a rotating refresh token and persist only the refresh hash. Encoding
    runs BEFORE the refresh row is added, so an unset signing secret (→ 500) leaves nothing persisted
    and issues no token. No org resolution (super-admins are org-less)."""
    access_token = encode_access_token(
        sub=str(user.id),
        email=user.email,
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
    return IssuedSession(
        access_token=access_token,
        expires_in=settings.jwt_access_ttl_seconds,
        refresh_token=raw_refresh,
        refresh_max_age=settings.jwt_refresh_ttl_seconds,
    )


async def login_local(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings,
    email: str,
    password: str,
    *,
    ip: str = "unknown",
) -> LoginOutcome:
    """Validate local credentials and issue a session, in the fixed gate order (D5).

    (1) local_auth_enabled — before any DB/redis/hash. (2) lockout check_locked — before the lookup.
    (3) lookup by lower(email) in admin_users — a DB error propagates as 500, NEVER remapped to 401.
    (4) uniform credential verify — unknown-email / no-hash / wrong-password all do equivalent
    Argon2id work and raise the identical InvalidCredentialsError, funneled through one except that
    records the lockout failure. (5) deactivation gate — only AFTER a correct password, reads the
    dedicated `active` column. (6) issue session — clear lockout, stamp last_login_at, mint tokens
    (NO org resolution).
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
                select(AdminUser).where(func.lower(AdminUser.email) == normalized)
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

        # (5) Deactivation gate — after verify, so a wrong password against a deactivated account is
        # indistinguishable from any other wrong password (never USER_DEACTIVATED). Reads the
        # dedicated `active` boolean column.
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

        user = await session.get(AdminUser, row.user_id)
        if user is None:
            raise SessionExpiredError()

        # Active gate: mirrors Org CP — a deactivated super-admin must not mint a fresh access
        # token via refresh; revoke the rest of the family and treat it as an expired session.
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
    """Change the authenticated super-admin's password (D5 order).

    Verify current (uniform 401 on a miss, dummy_verify for a no-hash account) → policy check →
    reject a no-op (PASSWORD_UNCHANGED) → re-hash + clear must_change_password + revoke ALL the
    super-admin's refresh tokens, in one transaction.
    """
    async with sessionmaker() as session:
        user = await session.get(AdminUser, user_id)
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
