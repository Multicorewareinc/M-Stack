"""Org CP internal key-verification endpoint (SP-02, add-mg-api-key-verification). Behind the
model-gateway's own narrow bearer (require_mg_service_key — distinct from the broad service key,
council review fix); a key hash resolves to its minimal verify record. Uses the ASGI idiom so
seeding + requests share the injected aiosqlite engine."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from conftest import SERVICE_KEY, asgi_client, auth_headers, init_schema, make_auth_app, mg_headers

from modules.api_keys import keygen


async def _seed_key(sessionmaker, *, owner_status="active", with_owner=True, revoked=False):
    """Insert an org + (optional) owner user + an api_keys row. Returns (raw, key_hash, org_id)."""
    from modules.api_keys.models import ApiKey
    from modules.organizations.models import Organization
    from modules.users.models import User

    org_id, owner_id = uuid.uuid4(), uuid.uuid4()
    raw, prefix, key_hash = keygen.generate_raw_key()
    async with sessionmaker() as s:
        s.add(Organization(id=org_id, name="Acme", status="active"))
        if with_owner:
            s.add(User(id=owner_id, organization_id=org_id, username="o", email="o@a.com", status=owner_status))
        s.add(ApiKey(
            org_id=org_id, owner_id=owner_id, name="k", key_hash=key_hash, prefix=prefix,
            revoked_at=datetime.now(timezone.utc) if revoked else None,
        ))
        await s.commit()
    return raw, key_hash, org_id, owner_id


async def test_verify_known_hash_returns_record():
    app, engine = make_auth_app()
    await init_schema(engine)
    raw, key_hash, org_id, owner_id = await _seed_key(app.state.sessionmaker)
    async with asgi_client(app) as c:
        r = await c.get("/internal/v1/api-keys/verify", params={"hash": key_hash}, headers=mg_headers())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["org_id"] == str(org_id)
    assert body["owner_id"] == str(owner_id)
    assert body["owner_active"] is True
    assert body["revoked_at"] is None
    assert "raw_key" not in body and raw not in r.text  # never echoes the raw/hash-derived secret


async def test_verify_unknown_hash_404():
    app, engine = make_auth_app()
    await init_schema(engine)
    async with asgi_client(app) as c:
        r = await c.get("/internal/v1/api-keys/verify", params={"hash": "0" * 64}, headers=mg_headers())
    assert r.status_code == 404


async def test_verify_deleted_owner_inactive():
    app, engine = make_auth_app()
    await init_schema(engine)
    # Key whose owner row does not exist ⇒ owner_active must be false (a missing owner is not active).
    raw, key_hash, org_id, owner_id = await _seed_key(app.state.sessionmaker, with_owner=False)
    async with asgi_client(app) as c:
        r = await c.get("/internal/v1/api-keys/verify", params={"hash": key_hash}, headers=mg_headers())
    assert r.status_code == 200
    assert r.json()["owner_active"] is False


async def test_verify_suspended_owner_inactive():
    app, engine = make_auth_app()
    await init_schema(engine)
    _, key_hash, _, _ = await _seed_key(app.state.sessionmaker, owner_status="suspended")
    async with asgi_client(app) as c:
        r = await c.get("/internal/v1/api-keys/verify", params={"hash": key_hash}, headers=mg_headers())
    assert r.json()["owner_active"] is False


async def test_verify_requires_service_bearer():
    app, engine = make_auth_app()
    await init_schema(engine)
    _, key_hash, _, _ = await _seed_key(app.state.sessionmaker)
    async with asgi_client(app) as c:
        r = await c.get("/internal/v1/api-keys/verify", params={"hash": key_hash})  # no bearer
    assert r.status_code == 401


async def test_verify_rejects_broad_service_key():
    """require_mg_service_key must reject the BROAD service_api_key — least-privilege: a caller
    holding only the /v1 credential must not reach the model-gateway's verify surface
    (council review fix; previously both were the same key/dependency)."""
    app, engine = make_auth_app()
    await init_schema(engine)
    _, key_hash, _, _ = await _seed_key(app.state.sessionmaker)
    async with asgi_client(app) as c:
        r = await c.get(
            "/internal/v1/api-keys/verify", params={"hash": key_hash}, headers=auth_headers(SERVICE_KEY)
        )
    assert r.status_code == 401
