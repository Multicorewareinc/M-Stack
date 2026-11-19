"""The e2e-only SEED_ADMIN_EMAIL/SEED_ADMIN_PASSWORD lifespan bootstrap (main.create_app): when both
are set a login-able super-admin exists (must_change_password gated); when unset none is created; and
a reboot against the same DB is idempotent (single-use guard refuses quietly, never crashes startup).
Mirrors the Org CP SEED_API_KEY seeder test."""

from __future__ import annotations

from conftest import JWT_SECRET, make_client

EMAIL = "superadmin@platform.local"
PASSWORD = "ChangeMe123!"


def _login(client, email=EMAIL, password=PASSWORD):
    return client.post("/api/auth/login", json={"email": email, "password": password})


def test_seed_admin_bootstraps_login_able_super_admin(fake_redis):
    client, _ = make_client(
        seed_admin_email=EMAIL, seed_admin_password=PASSWORD, jwt_secret=JWT_SECRET
    )
    with client:
        r = _login(client)
    assert r.status_code == 200
    assert r.json()["must_change_password"] is True  # gated to change-password until rotated (SP-02)


def test_seed_admin_idempotent_across_restart(fake_redis):
    client, engine = make_client(
        seed_admin_email=EMAIL, seed_admin_password=PASSWORD, jwt_secret=JWT_SECRET
    )
    with client:
        assert _login(client).status_code == 200
    # Reboot against the SAME engine: the single-use guard must refuse quietly (AlreadyBootstrapped),
    # not fail startup or create a second admin.
    client2, _ = make_client(
        engine=engine, seed_admin_email=EMAIL, seed_admin_password=PASSWORD, jwt_secret=JWT_SECRET
    )
    with client2:
        assert _login(client2).status_code == 200


def test_no_seed_admin_when_unset(fake_redis):
    client, _ = make_client(jwt_secret=JWT_SECRET)  # neither seed setting provided
    with client:
        assert _login(client).status_code == 401
