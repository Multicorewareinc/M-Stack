"""Admin-set initial/reset password on org user provisioning (follow-up from add-org-cp-local-auth's
SP-01 design open question: "how an org user's initial password is set"). ADR-025/AD-06: no public
self-register — an org admin sets the password when creating (or later resetting) a user via the
existing `/v1/users` surface; air-gapped, no self-service email reset (ADR-008 §5). Reuses the SP-01
Argon2id primitives; forces rotation (must_change_password=True), mirroring seed_admin."""

from __future__ import annotations

import uuid

from conftest import asgi_client, init_schema, make_auth_app, org_headers

PW = "Sup3rSecret!!x"


async def _app_and_org(**settings_kwargs):
    app, engine = make_auth_app(**settings_kwargs)
    await init_schema(engine)
    async with asgi_client(app) as c:
        org_id = str(uuid.uuid4())
        r = await c.post(
            "/internal/v1/organizations", json={"id": org_id, "name": "Acme"}, headers=org_headers(org_id)
        )
        assert r.status_code == 201
    return app, org_id


async def test_create_user_with_password_can_log_in_and_must_rotate(fake_redis):
    app, org_id = await _app_and_org()
    async with asgi_client(app) as c:
        created = await c.post(
            "/v1/users",
            json={"username": "alice", "email": "alice@acme.com", "password": PW},
            headers=org_headers(org_id),
        )
        assert created.status_code == 201
        body = created.json()
        assert "password" not in body and "password_hash" not in body  # never exposed

        login = await c.post("/api/auth/login", json={"email": "alice@acme.com", "password": PW})
        assert login.status_code == 200
        assert login.json()["must_change_password"] is True  # forced rotation, like seed_admin


async def test_create_user_without_password_cannot_log_in(fake_redis):
    app, org_id = await _app_and_org()
    async with asgi_client(app) as c:
        created = await c.post(
            "/v1/users",
            json={"username": "bob", "email": "bob@acme.com"},
            headers=org_headers(org_id),
        )
        assert created.status_code == 201  # unchanged prior behavior — password stays optional

        login = await c.post("/api/auth/login", json={"email": "bob@acme.com", "password": "whatever"})
        assert login.status_code == 401
        assert login.json()["error"]["type"] == "invalid_credentials"


async def test_create_with_weak_password_rejected_pre_hash(fake_redis):
    app, org_id = await _app_and_org()
    async with asgi_client(app) as c:
        r = await c.post(
            "/v1/users",
            json={"username": "carol", "email": "carol@acme.com", "password": "short"},
            headers=org_headers(org_id),
        )
        assert r.status_code == 422
        assert r.json()["error"]["type"] == "weak_password"


async def test_admin_reset_password_forces_rotation_and_new_password_works(fake_redis):
    app, org_id = await _app_and_org()
    async with asgi_client(app) as c:
        created = await c.post(
            "/v1/users",
            json={"username": "dave", "email": "dave@acme.com", "password": PW},
            headers=org_headers(org_id),
        )
        user_id = created.json()["id"]

        # Rotate once so must_change_password is false, confirming the reset flips it back to true.
        login = await c.post("/api/auth/login", json={"email": "dave@acme.com", "password": PW})
        token = login.json()["access_token"]
        rotate = await c.post(
            "/api/auth/password",
            headers={"Authorization": f"Bearer {token}"},
            json={"current_password": PW, "new_password": "some other passphrase 1"},
        )
        assert rotate.status_code == 204

        new_pw = "admin reset passphrase 2"
        reset = await c.patch(
            f"/v1/users/{user_id}", json={"password": new_pw}, headers=org_headers(org_id)
        )
        assert reset.status_code == 200

        old_login = await c.post(
            "/api/auth/login", json={"email": "dave@acme.com", "password": "some other passphrase 1"}
        )
        assert old_login.status_code == 401  # old password no longer works

        new_login = await c.post("/api/auth/login", json={"email": "dave@acme.com", "password": new_pw})
        assert new_login.status_code == 200
        assert new_login.json()["must_change_password"] is True  # admin reset forces rotation again


async def test_update_with_weak_password_rejected(fake_redis):
    app, org_id = await _app_and_org()
    async with asgi_client(app) as c:
        created = await c.post(
            "/v1/users",
            json={"username": "erin", "email": "erin@acme.com", "password": PW},
            headers=org_headers(org_id),
        )
        user_id = created.json()["id"]
        r = await c.patch(f"/v1/users/{user_id}", json={"password": "x"}, headers=org_headers(org_id))
        assert r.status_code == 422
        assert r.json()["error"]["type"] == "weak_password"


# ── Cross-org local-auth email uniqueness (migration 0005) ──────────────────────────────────────
# Local-auth login has no org context (it runs before any org is known), so it looks up a user by
# email GLOBALLY. Email is only unique PER ORG, so without this constraint two orgs could each have
# a password-having user sharing an email, and login would silently resolve to an arbitrary one of
# them. These tests pin: (a) the collision is rejected at the API boundary, (b) the pre-existing
# no-password cross-org email reuse (test_users.py::test_email_unique_per_org) is unaffected.


async def _second_org(client, name="Beta"):
    org_id = str(uuid.uuid4())
    r = await client.post(
        "/internal/v1/organizations", json={"id": org_id, "name": name}, headers=org_headers(org_id)
    )
    assert r.status_code == 201
    return org_id


async def test_cross_org_same_email_with_password_conflicts(fake_redis):
    app, org_a = await _app_and_org()
    async with asgi_client(app) as c:
        first = await c.post(
            "/v1/users",
            json={"username": "shared", "email": "shared@multi.org", "password": PW},
            headers=org_headers(org_a),
        )
        assert first.status_code == 201

        org_b = await _second_org(c)
        second = await c.post(
            "/v1/users",
            json={"username": "shared2", "email": "shared@multi.org", "password": "Different!!Pw2"},
            headers=org_headers(org_b),
        )
        assert second.status_code == 409
        assert second.json()["error"]["type"] == "conflict"
        assert second.json()["error"]["field"] == "password"


async def test_cross_org_same_email_without_password_still_allowed(fake_redis):
    """Regression guard: org-directory rows with no password may still share an email across
    orgs (pre-existing behavior, test_users.py::test_email_unique_per_org) — the new partial index
    must not affect this case."""
    app, org_a = await _app_and_org()
    async with asgi_client(app) as c:
        first = await c.post(
            "/v1/users",
            json={"username": "nopass1", "email": "nopass@multi.org"},
            headers=org_headers(org_a),
        )
        assert first.status_code == 201

        org_b = await _second_org(c)
        second = await c.post(
            "/v1/users",
            json={"username": "nopass2", "email": "nopass@multi.org"},
            headers=org_headers(org_b),
        )
        assert second.status_code == 201  # no password on either side -> no collision


async def test_cross_org_email_collision_via_update_also_conflicts(fake_redis):
    """The collision can also be introduced by an update (changing email on an already
    password-having row), not just at creation — has_password reflects the row's POST-update
    state, not just whether this call itself set a password."""
    app, org_a = await _app_and_org()
    async with asgi_client(app) as c:
        first = await c.post(
            "/v1/users",
            json={"username": "owner", "email": "taken@multi.org", "password": PW},
            headers=org_headers(org_a),
        )
        assert first.status_code == 201

        org_b = await _second_org(c)
        other = await c.post(
            "/v1/users",
            json={"username": "other", "email": "other@multi.org", "password": "Different!!Pw2"},
            headers=org_headers(org_b),
        )
        other_id = other.json()["id"]

        # Renaming `other`'s email to the already-taken one, with a password already set, collides.
        r = await c.patch(
            f"/v1/users/{other_id}", json={"email": "taken@multi.org"}, headers=org_headers(org_b)
        )
        assert r.status_code == 409
        assert r.json()["error"]["field"] == "password"
