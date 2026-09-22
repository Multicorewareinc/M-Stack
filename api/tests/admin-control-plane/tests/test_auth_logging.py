"""No credential material in emitted logs (spec 'Secrets are never logged' + task 9.6).

Drives login (success + failure), refresh, logout, and change-password while capturing every log
record, then asserts no password, password hash, raw/hashed refresh token, or signing secret appears.
"""

from __future__ import annotations

import hashlib
import logging

from conftest import JWT_SECRET, asgi_client, init_schema, make_auth_app, seed_admin
from modules.auth.models import AdminUser

PW = "L0gSecretPass!!"


async def test_no_secret_material_in_logs(fake_redis, caplog):
    caplog.set_level(logging.DEBUG)
    app, engine = make_auth_app()
    await init_schema(engine)
    sm = app.state.sessionmaker
    uid, _pw = await seed_admin(sm, email="log@platform.local", password=PW)
    new_pw = "R0tatedLogPass!!"

    async with asgi_client(app) as c:
        # success login → capture the refresh token issued
        c.cookies.clear()
        r = await c.post("/api/auth/login", json={"email": "log@platform.local", "password": PW})
        access = r.json()["access_token"]
        raw_refresh = r.cookies["refresh"]
        # a failed login (wrong password)
        c.cookies.clear()
        await c.post("/api/auth/login", json={"email": "log@platform.local", "password": "wrong-but-long"})
        # refresh + logout
        c.cookies.clear()
        rot = await c.post("/api/auth/refresh", cookies={"refresh": raw_refresh})
        rotated = rot.cookies["refresh"]
        c.cookies.clear()
        await c.post("/api/auth/logout", cookies={"refresh": rotated})
        # change-password
        await c.post(
            "/api/auth/password",
            json={"current_password": PW, "new_password": new_pw},
            headers={"Authorization": f"Bearer {access}"},
        )

    async with sm() as s:
        user = await s.get(AdminUser, uid)
        stored_hash = user.password_hash

    text = caplog.text
    forbidden = [
        PW,
        new_pw,
        stored_hash,
        raw_refresh,
        rotated,
        hashlib.sha256(raw_refresh.encode("ascii")).hexdigest(),
        hashlib.sha256(rotated.encode("ascii")).hexdigest(),
        JWT_SECRET,
    ]
    for secret in forbidden:
        assert secret not in text, f"secret material leaked into logs: {secret[:12]}..."
