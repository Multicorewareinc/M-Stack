"""API-key lifecycle logic (ADR-027/ADR-028). All operations are tenant-scoped: `org_id` comes from
the authenticated principal (the router), never from client input, and every by-id operation filters
`WHERE id AND org_id` so a cross-org key resolves to 404 (existence is never confirmed to an outsider,
reference §12.4).

This module owns its own session + commit (like modules/auth/service.py) rather than the per-request
`get_session` dependency, because eviction MUST run AFTER the commit (commit-then-evict, reference
§12.7) — a control the dependency's commit-on-return does not give. No httpx here (router→service→model).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from errors import ConflictError, NotFoundError, UnprocessableError
from modules.users.models import User

from . import cache
from .keygen import generate_raw_key
from .models import ApiKey
from .schemas import ApiKeyCreate, ApiKeyCreated, ApiKeyOut, ApiKeyUpdate, ApiKeyVerifyOut


def _created(row: ApiKey, raw: str) -> ApiKeyCreated:
    return ApiKeyCreated(
        id=row.id,
        org_id=row.org_id,
        owner_id=row.owner_id,
        name=row.name,
        prefix=row.prefix,
        created_at=row.created_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        raw_key=raw,
    )


async def _get_in_org(session: AsyncSession, org_id: uuid.UUID, key_id: uuid.UUID) -> ApiKey:
    """Load a key scoped to the caller's org. A miss (absent OR another org's) is a uniform 404."""
    row = await session.scalar(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.org_id == org_id)
    )
    if row is None:
        raise NotFoundError(f"API key {key_id} not found")
    return row


async def verify_by_hash(
    sessionmaker: async_sessionmaker[AsyncSession], key_hash: str
) -> ApiKeyVerifyOut:
    """Resolve a key hash to the minimal record the model-gateway re-validates (SP-02, ADR-026).
    `owner_active` is true only when the owning user row exists AND is active — a hard-deleted or
    suspended owner resolves to false (reference §10, so a removed member's key stops verifying). A
    hash matching no key row raises NotFoundError (the gateway renders an opaque 401)."""
    async with sessionmaker() as session:
        row = await session.scalar(select(ApiKey).where(ApiKey.key_hash == key_hash))
        if row is None:
            raise NotFoundError("API key not found")
        owner = await session.get(User, row.owner_id)
        owner_active = owner is not None and owner.status == "active"
    return ApiKeyVerifyOut(
        id=row.id,
        org_id=row.org_id,
        owner_id=row.owner_id,
        revoked_at=row.revoked_at,
        expires_at=row.expires_at,
        owner_active=owner_active,
    )


async def create_key(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings,
    org_id: uuid.UUID,
    owner_id: uuid.UUID,
    data: ApiKeyCreate,
) -> ApiKeyCreated:
    raw, prefix, key_hash = generate_raw_key()
    async with sessionmaker() as session:
        row = ApiKey(
            org_id=org_id,
            owner_id=owner_id,
            name=data.name,
            key_hash=key_hash,
            prefix=prefix,
            expires_at=data.expires_at,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
    # Nothing is cached for a brand-new key, so no eviction needed. Raw returned exactly once.
    return _created(row, raw)


async def list_keys(
    sessionmaker: async_sessionmaker[AsyncSession], org_id: uuid.UUID
) -> list[ApiKeyOut]:
    async with sessionmaker() as session:
        rows = await session.scalars(
            select(ApiKey).where(ApiKey.org_id == org_id).order_by(ApiKey.created_at.desc())
        )
        return [ApiKeyOut.model_validate(r) for r in rows]


async def rotate_key(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings,
    org_id: uuid.UUID,
    key_id: uuid.UUID,
) -> ApiKeyCreated:
    raw, prefix, key_hash = generate_raw_key()
    async with sessionmaker() as session:
        row = await _get_in_org(session, org_id, key_id)
        old_hash = row.key_hash
        row.key_hash = key_hash
        row.prefix = prefix
        await session.commit()
        await session.refresh(row)
    await cache.evict(settings, old_hash)  # commit-then-evict the OLD hash
    return _created(row, raw)


async def update_key(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings,
    org_id: uuid.UUID,
    key_id: uuid.UUID,
    data: ApiKeyUpdate,
) -> ApiKeyOut:
    fields = data.model_fields_set
    if "name" in fields and data.name is None:
        raise UnprocessableError("name cannot be cleared", field="name")
    async with sessionmaker() as session:
        row = await _get_in_org(session, org_id, key_id)
        if row.revoked_at is not None:
            raise ConflictError("cannot update a revoked API key")
        if "name" in fields:
            row.name = data.name
        if "expires_at" in fields:  # explicit null clears; absent leaves unchanged
            row.expires_at = data.expires_at
        key_hash = row.key_hash
        await session.commit()
        await session.refresh(row)
        out = ApiKeyOut.model_validate(row)
    await cache.evict(settings, key_hash)  # commit-then-evict
    return out


async def revoke_key(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings,
    org_id: uuid.UUID,
    key_id: uuid.UUID,
) -> None:
    async with sessionmaker() as session:
        row = await _get_in_org(session, org_id, key_id)
        key_hash = row.key_hash
        if row.revoked_at is None:  # idempotent: re-revoke is a no-op
            row.revoked_at = datetime.now(UTC)
            await session.commit()
    await cache.evict(settings, key_hash)  # commit-then-evict (best-effort even on re-revoke)
