"""The SEED_API_KEY seeder (src/seed.py): when set, the fixed verify key resolves through the
model-gateway's internal verify contract against a real, active-owned row; when unset, nothing is
seeded. Mirrors admin-control-plane's seed tests (ADR-019)."""

from __future__ import annotations

from conftest import make_client, mg_headers
from modules.api_keys.keygen import hash_key

FIXED_KEY = "sk-e2e-fixed-verify-key-001"


def _verify(client):
    return client.get(
        "/internal/v1/api-keys/verify",
        params={"hash": hash_key(FIXED_KEY)},
        headers=mg_headers(),
    )


def test_seed_resolves_via_internal_verify():
    client, _ = make_client(seed_api_key=FIXED_KEY)
    with client:
        r = _verify(client)
    assert r.status_code == 200
    body = r.json()
    assert body["owner_active"] is True  # the key is only usable because its owner is active
    assert body["revoked_at"] is None
    assert body["expires_at"] is None  # seeded durable (never-expiring)


def test_seed_is_idempotent_across_restart():
    # The injected StaticPool engine survives lifespan exit in tests (owns_engine=False), so re-entering
    # runs the seeder a second time against the existing row — it must skip, not duplicate or raise.
    client, _ = make_client(seed_api_key=FIXED_KEY)
    with client:
        assert _verify(client).status_code == 200
    with client:
        assert _verify(client).status_code == 200


def test_no_seed_when_unset():
    client, _ = make_client()  # seed_api_key defaults to None
    with client:
        assert _verify(client).status_code == 404
