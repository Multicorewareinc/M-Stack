"""Offline unit tests for the seed_admin bootstrap CLI (SP-03; add-admin-seed-bootstrap).

Uses an injected in-memory aiosqlite sessionmaker (the advisory lock is a Postgres-only no-op here —
design D3; real single-use under concurrency is covered by integration/test_seed_admin_pg.py). Maps
spec scenarios: bootstrap-fresh, second-run-refuse, weak-password-pre-DB (no hashing), missing-input,
transient-fail-closed, and no-secret-in-logs.
"""

from __future__ import annotations

import pytest

import seed_admin
from conftest import make_engine, init_schema

from db import build_sessionmaker
from errors import WeakPasswordError
from modules.auth.models import AdminUser
from modules.auth.password import verify_password
from sqlalchemy import func, select

SEED_EMAIL = "Root@Site.Local"
SEED_PASSWORD = "correct horse battery"  # ≥ 12 chars


async def _sessionmaker():
    engine = make_engine()
    await init_schema(engine)
    return build_sessionmaker(engine), engine


async def _count(sm) -> int:
    async with sm() as s:
        return await s.scalar(select(func.count()).select_from(AdminUser))


async def test_bootstrap_creates_exactly_one():
    sm, _ = await _sessionmaker()
    admin = await seed_admin.bootstrap_super_admin(SEED_EMAIL, SEED_PASSWORD, sessionmaker=sm)
    assert admin.email == "root@site.local"  # normalized (strip + lower)
    assert admin.must_change_password is True
    assert admin.active is True
    assert admin.password_hash and verify_password(SEED_PASSWORD, admin.password_hash)
    assert await _count(sm) == 1


async def test_second_run_refuses_and_creates_no_second_admin():
    sm, _ = await _sessionmaker()
    await seed_admin.bootstrap_super_admin(SEED_EMAIL, SEED_PASSWORD, sessionmaker=sm)
    with pytest.raises(seed_admin.AlreadyBootstrappedError):
        await seed_admin.bootstrap_super_admin("other@site.local", SEED_PASSWORD, sessionmaker=sm)
    assert await _count(sm) == 1


async def test_weak_password_rejected_before_any_hashing(monkeypatch):
    sm, _ = await _sessionmaker()
    calls = []
    monkeypatch.setattr(
        "modules.auth.password.hash_password", lambda raw: calls.append(raw) or "x"
    )
    with pytest.raises(WeakPasswordError):
        await seed_admin.bootstrap_super_admin(SEED_EMAIL, "short", sessionmaker=sm)
    assert calls == []  # policy runs before hashing → no hash work
    assert await _count(sm) == 0


async def test_no_secret_in_logs(caplog):
    sm, _ = await _sessionmaker()
    with caplog.at_level("INFO"):
        await seed_admin.bootstrap_super_admin(SEED_EMAIL, SEED_PASSWORD, sessionmaker=sm)
    assert SEED_PASSWORD not in caplog.text


def test_main_missing_input_exits_2(monkeypatch):
    monkeypatch.delenv("SEED_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("SEED_ADMIN_PASSWORD", raising=False)
    assert seed_admin.main([]) == seed_admin.EXIT_REFUSED


def test_main_success_exits_0(monkeypatch):
    async def _fake(email, password, **_):
        class _A:
            email = "root@site.local"
            id = "00000000-0000-0000-0000-000000000001"
        return _A()

    monkeypatch.setattr(seed_admin, "bootstrap_super_admin", _fake)
    assert seed_admin.main(["--email", SEED_EMAIL, "--password", SEED_PASSWORD]) == seed_admin.EXIT_OK


def test_main_already_bootstrapped_exits_2(monkeypatch):
    async def _fake(*a, **k):
        raise seed_admin.AlreadyBootstrappedError()

    monkeypatch.setattr(seed_admin, "bootstrap_super_admin", _fake)
    assert seed_admin.main(["--email", SEED_EMAIL, "--password", SEED_PASSWORD]) == seed_admin.EXIT_REFUSED


def test_main_transient_db_error_exits_3(monkeypatch):
    from sqlalchemy.exc import OperationalError

    async def _fake(*a, **k):
        raise OperationalError("stmt", {}, Exception("db down"))

    monkeypatch.setattr(seed_admin, "bootstrap_super_admin", _fake)
    assert seed_admin.main(["--email", SEED_EMAIL, "--password", SEED_PASSWORD]) == seed_admin.EXIT_TRANSIENT
