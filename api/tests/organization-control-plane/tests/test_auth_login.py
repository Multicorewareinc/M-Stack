"""Endpoint: POST /api/auth/login — success + cookie flags + org claim + last_login_at, no
account enumeration, deactivation-after-verify, local-auth-disabled short-circuit, DB-error fails
closed (500 not 401), over-length password rejected pre-hash. Maps spec + tasks 9.4."""

from __future__ import annotations

import uuid

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from conftest import (
    JWT_SECRET,
    asgi_client,
    init_schema,
    make_admin_client,
    make_auth_app,
    seed_user,
)
from modules.auth import jwt as auth_jwt
from modules.auth import service
from modules.users.models import User

PW = "Sup3rSecret!!x"


async def _app(**settings_kwargs):
    app, engine = make_auth_app(**settings_kwargs)
    await init_schema(engine)
    return app


async def test_login_success_cookie_org_claim_last_login(fake_redis):
    app = await _app()
    sm = app.state.sessionmaker
    uid, org_id, pw = await seed_user(sm, email="alice@acme.com", username="alice", password=PW)
    async with asgi_client(app) as c:
        r = await c.post("/api/auth/login", json={"email": "alice@acme.com", "password": pw})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 3600
    assert body["user"]["email"] == "alice@acme.com"
    assert body["org"]["id"] == str(org_id)
    assert body["must_change_password"] is False
    # Access token carries sub/email/role/org (decoded with the configured secret).
    claims = auth_jwt.decode_access_token(body["access_token"], secret=SecretStr(JWT_SECRET))
    assert claims["sub"] == str(uid) and claims["role"] == "org_user"
    assert claims["org"] == str(org_id)
    # Refresh cookie: HttpOnly; Secure; SameSite=Strict; Path=/api/auth.
    set_cookie = r.headers["set-cookie"].lower()
    assert "refresh=" in set_cookie
    assert "httponly" in set_cookie and "secure" in set_cookie
    assert "samesite=strict" in set_cookie and "path=/api/auth" in set_cookie
    # last_login_at stamped.
    async with sm() as s:
        u = await s.get(User, uid)
        assert u.last_login_at is not None


async def test_no_enumeration_identical_401(fake_redis, monkeypatch):
    """Unknown email, no-hash account, and wrong password all return a byte-identical 401, and each
    performs equivalent Argon2id work (dummy_verify for the first two, a real verify for the third)."""
    app = await _app()
    sm = app.state.sessionmaker
    org_id = uuid.uuid4()
    await seed_user(sm, org_id=org_id, email="real@acme.com", username="real", password=PW)
    await seed_user(sm, org_id=org_id, email="nohash@acme.com", username="nohash", with_hash=False)

    dummy_calls = {"n": 0}
    verify_calls = {"n": 0}
    orig_dummy, orig_verify = service.dummy_verify, service.verify_password
    monkeypatch.setattr(
        service, "dummy_verify", lambda: (dummy_calls.__setitem__("n", dummy_calls["n"] + 1), orig_dummy())[1]
    )
    monkeypatch.setattr(
        service,
        "verify_password",
        lambda raw, h: (verify_calls.__setitem__("n", verify_calls["n"] + 1), orig_verify(raw, h))[1],
    )

    async with asgi_client(app) as c:
        r_unknown = await c.post("/api/auth/login", json={"email": "ghost@acme.com", "password": PW})
        r_nohash = await c.post("/api/auth/login", json={"email": "nohash@acme.com", "password": PW})
        r_wrong = await c.post("/api/auth/login", json={"email": "real@acme.com", "password": "wrong-but-long"})

    for r in (r_unknown, r_nohash, r_wrong):
        assert r.status_code == 401
        assert r.json() == {"error": {"message": "The email or password is incorrect.", "type": "invalid_credentials"}}
    assert dummy_calls["n"] == 2  # unknown-email + no-hash branches
    assert verify_calls["n"] == 1  # wrong-password branch does a real Argon2 verify


async def test_deactivation_after_verify(fake_redis):
    app = await _app()
    sm = app.state.sessionmaker
    await seed_user(sm, email="susp@acme.com", username="susp", password=PW, status="suspended")
    async with asgi_client(app) as c:
        # Correct password against a suspended user → USER_DEACTIVATED (only after verify).
        good = await c.post("/api/auth/login", json={"email": "susp@acme.com", "password": PW})
        # Wrong password against the same suspended user → indistinguishable INVALID_CREDENTIALS.
        bad = await c.post("/api/auth/login", json={"email": "susp@acme.com", "password": "wrong-but-long"})
    assert good.status_code == 401 and good.json()["error"]["type"] == "user_deactivated"
    assert bad.status_code == 401 and bad.json()["error"]["type"] == "invalid_credentials"


async def test_local_auth_disabled_short_circuits(fake_redis, monkeypatch):
    app = await _app(local_auth_enabled=False)
    sm = app.state.sessionmaker
    await seed_user(sm, email="x@acme.com", username="x", password=PW)
    # Assert no DB lookup / no lockout check happens: spy on check_locked.
    from modules.auth import lockout

    called = {"n": 0}
    orig = lockout.check_locked

    async def spy(*a, **k):
        called["n"] += 1
        return await orig(*a, **k)

    monkeypatch.setattr(lockout, "check_locked", spy)
    async with asgi_client(app) as c:
        r = await c.post("/api/auth/login", json={"email": "x@acme.com", "password": PW})
    assert r.status_code == 403 and r.json()["error"]["type"] == "local_auth_disabled"
    assert called["n"] == 0  # short-circuited before the lockout check (and any DB/hash work)


async def test_db_error_fails_closed_not_401(monkeypatch):
    """A DB error during the user lookup propagates (→ 500), never remapped to 401."""
    app, engine = make_auth_app()
    await init_schema(engine)
    settings = app.state.settings
    from modules.auth import lockout

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(lockout, "check_locked", _noop)

    class _BoomSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def scalar(self, *a, **k):
            raise OperationalError("SELECT", {}, Exception("db down"))

    def boom_sessionmaker():
        return _BoomSession()

    with pytest.raises(OperationalError):
        await service.login_local(boom_sessionmaker, settings, "a@acme.com", PW, ip="1.2.3.4")


async def test_lockout_trips_429_with_retry_after(fake_redis):
    """End-to-end: LOGIN_MAX_ATTEMPTS failures for the same email/IP trip a 429 TOO_MANY_ATTEMPTS
    with a Retry-After header, before any further lookup (spec 'Brute-force lockout')."""
    app = await _app(login_max_attempts=3)
    sm = app.state.sessionmaker
    await seed_user(sm, email="lock@acme.com", username="lock", password=PW)
    async with asgi_client(app) as c:
        for _ in range(3):
            r = await c.post("/api/auth/login", json={"email": "lock@acme.com", "password": "wrong-but-long"})
            assert r.status_code == 401
        locked = await c.post("/api/auth/login", json={"email": "lock@acme.com", "password": PW})
    assert locked.status_code == 429 and locked.json()["error"]["type"] == "too_many_attempts"
    assert int(locked.headers["retry-after"]) > 0


async def test_over_length_password_rejected_pre_hash(fake_redis, monkeypatch):
    app = await _app()
    # If schema validation lets it through, hashing would run — spy to prove it does NOT.
    monkeypatch.setattr(
        service, "verify_password", lambda *a, **k: pytest.fail("hasher invoked on over-length pw")
    )
    async with asgi_client(app) as c:
        r = await c.post("/api/auth/login", json={"email": "a@acme.com", "password": "p" * 4097})
    assert r.status_code == 422
