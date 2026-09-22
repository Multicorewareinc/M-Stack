"""Endpoint: refresh rotation + reuse-detection family-revoke, device-scoped idempotent logout,
change-password (revoke-all / PASSWORD_UNCHANGED / WEAK_PASSWORD / forced-rotation bypass), and
/me (super-admin identity, no org, forced-rotation gate). Maps spec + task 9.5."""

from __future__ import annotations

from sqlalchemy import func, select

from conftest import asgi_client, init_schema, make_auth_app, seed_admin
from modules.auth.models import RefreshToken

PW = "Sup3rSecret!!x"


async def _app(**settings_kwargs):
    app, engine = make_auth_app(**settings_kwargs)
    await init_schema(engine)
    return app


async def _login(client, email, password):
    client.cookies.clear()
    r = await client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json(), r.cookies["refresh"]


async def _with_cookie(client, path, token=None):
    """POST `path` presenting exactly `token` as the refresh cookie. The jar is emptied first so the
    per-request cookie is the ONLY one sent (no ambiguous merge with a prior response's cookie)."""
    client.cookies.clear()
    r = await client.post(path, cookies={"refresh": token} if token is not None else None)
    client.cookies.clear()  # drop any rotated cookie so the next call controls what it presents
    return r


async def test_refresh_rotates_token(fake_redis):
    app = await _app()
    sm = app.state.sessionmaker
    await seed_admin(sm, email="r@platform.local", password=PW)
    async with asgi_client(app) as c:
        _, refresh1 = await _login(c, "r@platform.local", PW)
        r = await _with_cookie(c, "/api/auth/refresh", refresh1)
        assert r.status_code == 200
        refresh2 = r.cookies["refresh"]
        assert refresh2 != refresh1
        # The new token works (rotates again).
        r3 = await _with_cookie(c, "/api/auth/refresh", refresh2)
        assert r3.status_code == 200 and r3.cookies["refresh"] != refresh2
        # The old token no longer authenticates (revoked → session_expired).
        replay = await _with_cookie(c, "/api/auth/refresh", refresh1)
        assert replay.status_code == 401 and replay.json()["error"]["type"] == "session_expired"


async def test_refresh_missing_or_unknown_401(fake_redis):
    app = await _app()
    async with asgi_client(app) as c:
        assert (await _with_cookie(c, "/api/auth/refresh")).status_code == 401
        bad = await _with_cookie(c, "/api/auth/refresh", "no-such-token")
        assert bad.status_code == 401 and bad.json()["error"]["type"] == "session_expired"


async def test_reuse_detection_revokes_family(fake_redis):
    app = await _app()
    sm = app.state.sessionmaker
    uid, _pw = await seed_admin(sm, email="f@platform.local", password=PW)
    async with asgi_client(app) as c:
        _, refresh1 = await _login(c, "f@platform.local", PW)
        rot = await _with_cookie(c, "/api/auth/refresh", refresh1)
        refresh2 = rot.cookies["refresh"]  # live successor
        # Replay the already-revoked refresh1 → stolen-cookie signal → family revoke + 401.
        replay = await _with_cookie(c, "/api/auth/refresh", refresh1)
        assert replay.status_code == 401 and replay.json()["error"]["type"] == "session_expired"
        # The whole family is now revoked — refresh2 no longer works.
        assert (await _with_cookie(c, "/api/auth/refresh", refresh2)).status_code == 401
    async with sm() as s:
        live = await s.scalar(
            select(func.count())
            .select_from(RefreshToken)
            .where(RefreshToken.user_id == uid, RefreshToken.revoked_at.is_(None))
        )
        assert live == 0


async def test_logout_device_scoped_and_idempotent(fake_redis):
    import hashlib

    app = await _app()
    sm = app.state.sessionmaker
    await seed_admin(sm, email="d@platform.local", password=PW)
    async with asgi_client(app) as c:
        _, dev_a = await _login(c, "d@platform.local", PW)
        _, dev_b = await _login(c, "d@platform.local", PW)  # second device
        out = await _with_cookie(c, "/api/auth/logout", dev_a)
        assert out.status_code == 204
        # Device B is untouched — it still authenticates (device-scoped revoke). We verify device A
        # via the DB rather than /refresh, because presenting a revoked token would trip the
        # reuse-detection family-revoke and take device B down with it.
        assert (await _with_cookie(c, "/api/auth/refresh", dev_b)).status_code == 200
        # Idempotent: logout with no cookie / an already-revoked token still 204.
        assert (await _with_cookie(c, "/api/auth/logout")).status_code == 204
        assert (await _with_cookie(c, "/api/auth/logout", dev_a)).status_code == 204
    async with sm() as s:
        a_hash = hashlib.sha256(dev_a.encode("ascii")).hexdigest()
        row = await s.scalar(select(RefreshToken).where(RefreshToken.token_hash == a_hash))
        assert row is not None and row.revoked_at is not None  # device A's token is revoked


async def test_change_password_revokes_all_and_rehashes(fake_redis):
    app = await _app()
    sm = app.state.sessionmaker
    await seed_admin(sm, email="cp@platform.local", password=PW)
    new_pw = "An0therStrongPass!!"
    async with asgi_client(app) as c:
        body, refresh1 = await _login(c, "cp@platform.local", PW)
        access = body["access_token"]
        r = await c.post(
            "/api/auth/password",
            json={"current_password": PW, "new_password": new_pw},
            headers={"Authorization": f"Bearer {access}"},
        )
        assert r.status_code == 204
        # All refresh tokens revoked.
        assert (await _with_cookie(c, "/api/auth/refresh", refresh1)).status_code == 401
        # Old password no longer works; new one does.
        assert (await c.post("/api/auth/login", json={"email": "cp@platform.local", "password": PW})).status_code == 401
        assert (await c.post("/api/auth/login", json={"email": "cp@platform.local", "password": new_pw})).status_code == 200


async def test_change_password_requires_bearer(fake_redis):
    """POST /api/auth/password is the sanctioned no-org/no-rotation bypass, but it is still an
    AUTHENTICATED route — no bearer, or an invalid one, must 401 (this specific route had no
    direct unauthorized-access test)."""
    app = await _app()
    async with asgi_client(app) as c:
        no_token = await c.post(
            "/api/auth/password", json={"current_password": PW, "new_password": "An0therStrongPass!!"}
        )
        bad_token = await c.post(
            "/api/auth/password",
            json={"current_password": PW, "new_password": "An0therStrongPass!!"},
            headers={"Authorization": "Bearer not-a-jwt"},
        )
    assert no_token.status_code == 401
    assert bad_token.status_code == 401


async def test_change_password_unchanged_and_weak(fake_redis):
    app = await _app()
    sm = app.state.sessionmaker
    await seed_admin(sm, email="cp2@platform.local", password=PW)
    async with asgi_client(app) as c:
        body, _ = await _login(c, "cp2@platform.local", PW)
        h = {"Authorization": f"Bearer {body['access_token']}"}
        # New == current → PASSWORD_UNCHANGED (422).
        same = await c.post("/api/auth/password", json={"current_password": PW, "new_password": PW}, headers=h)
        assert same.status_code == 422 and same.json()["error"]["type"] == "password_unchanged"
        # New below policy → WEAK_PASSWORD (422).
        weak = await c.post("/api/auth/password", json={"current_password": PW, "new_password": "short"}, headers=h)
        assert weak.status_code == 422 and weak.json()["error"]["type"] == "weak_password"
        # Wrong current → INVALID_CREDENTIALS (401).
        wrong = await c.post(
            "/api/auth/password", json={"current_password": "wrong-but-long", "new_password": "An0therStrongPass!!"}, headers=h
        )
        assert wrong.status_code == 401 and wrong.json()["error"]["type"] == "invalid_credentials"


async def test_forced_rotation_gate_and_bypass(fake_redis):
    app = await _app()
    sm = app.state.sessionmaker
    await seed_admin(sm, email="seed@platform.local", password=PW, must_change_password=True)
    new_pw = "Rot4tedStrongPass!!"
    async with asgi_client(app) as c:
        body, _ = await _login(c, "seed@platform.local", PW)
        assert body["must_change_password"] is True
        h = {"Authorization": f"Bearer {body['access_token']}"}
        # Gated route (/me) is blocked with PASSWORD_CHANGE_REQUIRED.
        me = await c.get("/api/auth/me", headers=h)
        assert me.status_code == 403 and me.json()["error"]["type"] == "password_change_required"
        # /password is the sanctioned bypass (un-gated) → 204.
        r = await c.post("/api/auth/password", json={"current_password": PW, "new_password": new_pw}, headers=h)
        assert r.status_code == 204
        # After rotation, log in again and /me works.
        body2, _ = await _login(c, "seed@platform.local", new_pw)
        assert body2["must_change_password"] is False
        me2 = await c.get("/api/auth/me", headers={"Authorization": f"Bearer {body2['access_token']}"})
        assert me2.status_code == 200


async def test_me_returns_super_admin_identity_no_org(fake_redis):
    app = await _app()
    sm = app.state.sessionmaker
    uid, _pw = await seed_admin(sm, email="m@platform.local", password=PW)
    async with asgi_client(app) as c:
        body, _ = await _login(c, "m@platform.local", PW)
        me = await c.get("/api/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200
    j = me.json()
    assert j["user"]["id"] == str(uid)
    assert j["user"]["email"] == "m@platform.local"
    assert j["user"]["role"] == "super_admin"
    assert "org" not in j and "permissions" not in j  # org-less super-admin identity


async def test_me_requires_valid_token(fake_redis):
    app = await _app()
    async with asgi_client(app) as c:
        assert (await c.get("/api/auth/me")).status_code == 401
        assert (await c.get("/api/auth/me", headers={"Authorization": "Bearer garbage"})).status_code == 401
