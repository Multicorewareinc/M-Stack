"""Admin CP organization user directory — synchronous read-through proxy to Org CP (SP-06).
Org CP is stubbed via MockTransport dispatching by path."""

from __future__ import annotations

import uuid

import httpx

from conftest import auth_headers, make_client

H = auth_headers()

ORG = str(uuid.uuid4())
USER = str(uuid.uuid4())
USER_LIST = [{"id": USER, "username": "john", "status": "active"}]
USER_ONE = {"id": USER, "username": "john", "email": "john@e.com", "status": "active"}
ROLES_VIEW = [{"role_id": str(uuid.uuid4()), "name": "Admin", "permission_ids": []}]


def make_dir_org_client(*, mode="ok", recorder=None):
    """Org CP stub: serves the internal user endpoints. mode: 'ok' | 'notfound' | 'down'."""

    def handler(request: httpx.Request) -> httpx.Response:
        if mode == "down":
            raise httpx.ConnectError("org cp down", request=request)
        if recorder is not None:
            recorder.append(request.url.path)
        if mode == "notfound":
            return httpx.Response(404, json={"error": {"message": "nope", "type": "not_found"}})
        path = request.url.path
        if path.endswith("/roles"):
            return httpx.Response(200, json=ROLES_VIEW)
        if path.endswith(f"/users/{USER}"):
            return httpx.Response(200, json=USER_ONE)
        if path.endswith("/users"):
            return httpx.Response(200, json=USER_LIST)
        return httpx.Response(404, json={"error": {"message": "nope", "type": "not_found"}})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://org-cp")


def test_list_users_proxies_org_cp():
    rec: list = []
    c, _ = make_client(org_client=make_dir_org_client(recorder=rec))
    with c:
        r = c.get(f"/v1/organizations/{ORG}/users", headers=H)
        assert r.status_code == 200 and r.json() == USER_LIST
        assert f"/internal/v1/organizations/{ORG}/users" in rec


def test_user_detail_proxies_org_cp():
    c, _ = make_client(org_client=make_dir_org_client())
    with c:
        r = c.get(f"/v1/organizations/{ORG}/users/{USER}", headers=H)
        assert r.status_code == 200 and r.json()["email"] == "john@e.com"


def test_user_roles_proxies_org_cp():
    rec: list = []
    c, _ = make_client(org_client=make_dir_org_client(recorder=rec))
    with c:
        r = c.get(f"/v1/organizations/{ORG}/users/{USER}/roles", headers=H)
        assert r.status_code == 200 and r.json()[0]["name"] == "Admin"
        # The proxy must forward the EXACT org+user from the path, not just any "*/roles" call —
        # the stub matches loosely (endswith), so this is the only thing that would catch the
        # proxy silently dropping/mis-scoping either id.
        assert rec == [f"/internal/v1/organizations/{ORG}/users/{USER}/roles"]


def test_org_cp_404_propagates():
    c, _ = make_client(org_client=make_dir_org_client(mode="notfound"))
    with c:
        r = c.get(f"/v1/organizations/{ORG}/users/{USER}", headers=H)
        assert r.status_code == 404 and r.json()["error"]["type"] == "not_found"


def test_org_cp_unavailable_502():
    c, _ = make_client(org_client=make_dir_org_client(mode="down"))
    with c:
        r = c.get(f"/v1/organizations/{ORG}/users", headers=H)
        assert r.status_code == 502 and r.json()["error"]["type"] == "upstream_error"


def test_directory_requires_admin_key():
    rec: list = []
    c, _ = make_client(org_client=make_dir_org_client(recorder=rec))
    with c:
        r = c.get(f"/v1/organizations/{ORG}/users")  # no bearer
        assert r.status_code == 401
        assert rec == []  # Org CP not called


# ---- Platform-wide (flat) directory — real shape, no org known ahead of time -----------------

PLATFORM_USER_LIST = [
    {"id": USER, "organization_id": ORG, "username": "john", "status": "active"},
]
PLATFORM_USER_ONE = {
    "id": USER,
    "organization_id": ORG,
    "username": "john",
    "email": "john@e.com",
    "display_name": "John",
    "status": "active",
}


def make_platform_org_client(*, permission_ids=(), mode="ok"):
    def handler(request: httpx.Request) -> httpx.Response:
        if mode == "down":
            raise httpx.ConnectError("org cp down", request=request)
        path = request.url.path
        if path == "/internal/v1/users":
            return httpx.Response(200, json=PLATFORM_USER_LIST)
        if path == f"/internal/v1/users/{USER}":
            if mode == "notfound":
                return httpx.Response(404, json={"error": {"message": "nope", "type": "not_found"}})
            return httpx.Response(200, json=PLATFORM_USER_ONE)
        if path.endswith("/roles"):
            return httpx.Response(
                200, json=[{"role_id": str(uuid.uuid4()), "name": "Admin", "permission_ids": list(permission_ids)}]
            )
        return httpx.Response(404, json={"error": {"message": "nope", "type": "not_found"}})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://org-cp")


def test_platform_list_proxies_org_cp():
    c, _ = make_client(org_client=make_platform_org_client())
    with c:
        r = c.get("/v1/users", headers=H)
        assert r.status_code == 200 and r.json() == PLATFORM_USER_LIST


def test_platform_detail_composes_roles_and_permission_slugs():
    # First open: seed/learn this db's own permission ids and slugs (the composition reads the
    # local permissions table). No known id yet, so the composed role's permission_ids is empty.
    probe, engine = make_client(org_client=make_platform_org_client())
    with probe:
        seeded = probe.get("/v1/permissions", headers=H).json()[0]
        known_id, known_slug = seeded["id"], seeded["slug"]

    # Reopen the SAME db with an org_client whose role view now carries that known id.
    c, _ = make_client(engine=engine, org_client=make_platform_org_client(permission_ids=[known_id]))
    with c:
        r = c.get(f"/v1/users/{USER}", headers=H)
        assert r.status_code == 200
        body = r.json()
        assert body["email"] == "john@e.com"
        assert body["roles"] == ["Admin"]
        assert body["effective_permissions"] == [known_slug]


def test_platform_detail_absent_user_404():
    c, _ = make_client(org_client=make_platform_org_client(mode="notfound"))
    with c:
        r = c.get(f"/v1/users/{USER}", headers=H)
        assert r.status_code == 404


def test_platform_detail_upstream_failure_502():
    c, _ = make_client(org_client=make_platform_org_client(mode="down"))
    with c:
        r = c.get(f"/v1/users/{USER}", headers=H)
        assert r.status_code == 502
