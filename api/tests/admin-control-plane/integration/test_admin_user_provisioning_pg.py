"""Postgres integration (AD-07) for super-admin org+owner provisioning (ADR-029/AD-02c, SP-03).

Runs the with-owner flow against a real Alembic-migrated Admin Postgres with a stubbed Org CP: the
org must be durably committed `active` and the owner create + org_admin assignment must be proxied.
(The Org CP-side persistence of the owner + role is covered by SP-02's test_user_provisioning_pg.py.)
Runs only with ADMIN_PG_DSN set.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

asyncpg = pytest.importorskip("asyncpg")

from sqlalchemy import delete, select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

DSN = os.environ.get("ADMIN_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="ADMIN_PG_DSN not set — Postgres integration only")


def _async_dsn(dsn: str) -> str:
    for prefix in ("postgresql://", "postgres://"):
        if dsn.startswith(prefix):
            return "postgresql+asyncpg://" + dsn[len(prefix):]
    return dsn


def _org_stub(recorder):
    created_user = {"id": str(uuid.uuid4()), "email": "owner@acme.com", "username": "owner"}
    role_id = str(uuid.uuid4())

    def handler(request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        recorder.append((method, path))
        if path == "/internal/v1/organizations" and method == "POST":
            return httpx.Response(201, json={"id": "stub", "name": "stub"})
        if path == "/v1/users" and method == "POST":
            return httpx.Response(201, json=created_user)
        if path == "/v1/roles" and method == "GET":
            return httpx.Response(200, json=[{"id": role_id, "name": "org_admin"}])
        if path.endswith("/roles") and method == "PUT":
            return httpx.Response(200, json={"role_ids": [role_id]})
        return httpx.Response(200, json={})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://org-cp")


def _billing_stub():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"outcome": "created", "stripe_subscription_id": "sub_stub"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://billing")


async def test_with_owner_persists_org_and_proxies_owner():
    from modules.organizations.models import Organization
    from modules.organizations.schemas import OrgCreate
    from modules.plans.models import Plan
    from modules.user_provisioning import service

    engine = create_async_engine(_async_dsn(DSN))
    sm = async_sessionmaker(engine, expire_on_commit=False)
    recorder: list = []
    org_client = _org_stub(recorder)
    billing_client = _billing_stub()
    plan_id = None
    org_id = None
    try:
        async with sm() as s:
            plan = Plan(name=f"plan-{uuid.uuid4()}", tpm=1, rpm=1, quota_monthly_tokens=1, is_default=False)
            s.add(plan)
            await s.commit()
            plan_id = plan.id

        result = await service.create_org_with_owner(
            sm,
            org_client,
            billing_client,
            OrgCreate(name=f"NewCo-{uuid.uuid4()}", plan_id=plan_id),
            {"username": "owner", "email": "owner@acme.com", "password": "Own3rPass!!x"},
        )
        org_id = uuid.UUID(result["organization"]["id"])

        # Org durably committed as active in Admin Postgres.
        async with sm() as s:
            org = await s.get(Organization, org_id)
            assert org is not None and org.status == "active"
        # Owner create + org_admin assignment were proxied to Org CP.
        assert ("POST", "/v1/users") in recorder
        assert any(m == "PUT" and p.endswith("/roles") for m, p in recorder)
    finally:
        async with sm() as s:
            if org_id is not None:
                await s.execute(delete(Organization).where(Organization.id == org_id))
            if plan_id is not None:
                await s.execute(delete(Plan).where(Plan.id == plan_id))
            await s.commit()
        await org_client.aclose()
        await billing_client.aclose()
        await engine.dispose()
