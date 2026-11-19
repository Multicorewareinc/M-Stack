"""Idempotent seed of a single fixed verify API key — its org and active owner included — run once on
lifespan startup when SEED_API_KEY is set. e2e/CI points the model-gateway at a fixed bearer
(`sk-e2e-fixed-verify-key-001`) so its auth path can be exercised without a human minting a key first;
this seeds the one row that bearer's SHA-256 hash resolves to (SP-02, ADR-026).

Disabled — a plain no-op — when SEED_API_KEY is empty, which is the production default, so no fixed
credential is ever seeded outside a test environment. Insert-missing only (by the unique key_hash and
by fixed org/owner ids), mirroring admin-control-plane's seed_plans (ADR-019): a reboot re-resolves
the same rows and never duplicates or overwrites them.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from modules.api_keys.keygen import hash_key
from modules.api_keys.models import ApiKey
from modules.organizations.models import Organization
from modules.users.models import User

# Deterministic ids so the seed is idempotent across reboots — the org/owner the fixed key resolves to
# are always the same rows. Clearly namespaced (the "e2e" prefix) so they are recognizable in the DB.
SEED_ORG_ID = uuid.UUID("e2e00000-0000-4000-8000-000000000001")
SEED_OWNER_ID = uuid.UUID("e2e00000-0000-4000-8000-000000000002")


async def seed_api_key(session: AsyncSession, raw_key: str | None) -> None:
    """Provision the fixed verify key `raw_key` (plus its org and active owner) if absent. A falsy
    `raw_key` disables seeding entirely (the production default). The owner is seeded `active` because
    verify_by_hash only reports `owner_active` true for an existing active owner (reference §10)."""
    if not raw_key:
        return
    key_hash = hash_key(raw_key)
    if await session.scalar(select(ApiKey.id).where(ApiKey.key_hash == key_hash)) is not None:
        return  # already seeded — insert-missing only (key_hash is unique regardless)

    if await session.get(Organization, SEED_ORG_ID) is None:
        session.add(Organization(id=SEED_ORG_ID, name="E2E Verify Org", status="active"))
    if await session.get(User, SEED_OWNER_ID) is None:
        session.add(
            User(
                id=SEED_OWNER_ID,
                organization_id=SEED_ORG_ID,
                username="e2e-verify",
                email="e2e-verify@platform.local",
                status="active",
            )
        )
    session.add(
        ApiKey(
            org_id=SEED_ORG_ID,
            owner_id=SEED_OWNER_ID,
            name="e2e-fixed-verify-key",
            key_hash=key_hash,
            prefix=raw_key[:11],  # display/log-safe fragment; never the full secret
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        # A concurrent replica (replicaCount > 1) won the race on the unique key_hash / fixed ids.
        # The row now exists either way, so treat this as already-seeded rather than crashing startup.
        await session.rollback()
