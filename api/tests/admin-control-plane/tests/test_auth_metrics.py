"""cp_auth_logins_total / cp_auth_lockouts_total across a lockout sequence."""

from __future__ import annotations

from conftest import asgi_client, init_schema, make_auth_app, seed_admin
from prometheus_client import REGISTRY

PW = "Sup3rSecret!!x"


def _logins(result):
    return REGISTRY.get_sample_value("cp_auth_logins_total", {"result": result}) or 0.0


def _lockouts():
    return REGISTRY.get_sample_value("cp_auth_lockouts_total") or 0.0


async def test_login_outcomes_and_lockouts_are_counted(fake_redis):
    app, engine = make_auth_app(login_max_attempts=3)
    await init_schema(engine)
    sm = app.state.sessionmaker
    await seed_admin(sm, email="ok@platform.local", password=PW)
    await seed_admin(sm, email="lock@platform.local", password=PW)
    before = {r: _logins(r) for r in ("success", "invalid_credentials", "locked")}
    lockouts_before = _lockouts()

    async with asgi_client(app) as c:
        assert (await c.post("/api/auth/login", json={"email": "ok@platform.local", "password": PW})).status_code == 200
        for _ in range(3):
            await c.post("/api/auth/login", json={"email": "lock@platform.local", "password": "wrong-but-long"})
        locked = await c.post("/api/auth/login", json={"email": "lock@platform.local", "password": PW})
    assert locked.status_code == 429

    assert _logins("success") == before["success"] + 1
    assert _logins("invalid_credentials") == before["invalid_credentials"] + 3
    assert _logins("locked") == before["locked"] + 1
    assert _lockouts() == lockouts_before + 1
