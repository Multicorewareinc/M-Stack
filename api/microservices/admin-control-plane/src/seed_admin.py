"""One-time super-admin bootstrap CLI (SP-03; add-admin-seed-bootstrap).

Creates exactly one org-less super-admin in `admin_users` on a fresh database so an air-gapped
install (ADR-007, no SMTP) can be brought online without a pre-existing identity (ADR-008). It is an
**operator-run CLI, never an HTTP endpoint** (bootstrap must not be reachable over the API).

Invocation (from the service `src/` dir, after `alembic upgrade head`)::

    SEED_ADMIN_EMAIL=admin@site SEED_ADMIN_PASSWORD=... python -m seed_admin

`--email` / `--password` override the env vars (interactive use only; prefer env/secret injection in
production to avoid shell-history exposure — see the deployment runbook).

Single-use semantics (design D2/D3): inside one transaction the script takes a transaction-scoped
PostgreSQL advisory lock, then inserts only when `admin_users` is empty. The lock serializes
concurrent invocations across connections, so even two simultaneous runs — same or different email —
yield exactly one admin; the `admin_users.email` UNIQUE constraint is a defence-in-depth backstop. On
any transient/DB failure the script fails closed and creates nothing.

The seed account has an Argon2id `password_hash` (via `modules.auth.password`), `must_change_password
= TRUE` (SP-02's gate confines it to change-password until rotated), and `active = TRUE`. Config, db,
and models are imported lazily inside functions, so importing this module is side-effect-free.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

logger = logging.getLogger(__name__)

# Fixed 64-bit advisory-lock key for the bootstrap critical section (design D3). A stable constant so
# every invocation contends on the same lock; the bytes spell "PERSADMN" and sit below the signed
# bigint max, a valid pg_advisory_xact_lock(bigint) argument.
_BOOTSTRAP_LOCK_KEY = 0x5045525341444D4E  # b"PERSADMN"

# Process exit codes (this is a CLI, not an HTTP surface):
EXIT_OK = 0          # one super_admin created
EXIT_REFUSED = 2     # single-use refusal / weak password / missing input — nothing created
EXIT_TRANSIENT = 3   # transient/DB failure — fail closed, nothing created


class SeedAdminError(Exception):
    """Base for bootstrap control-flow signals (CLI-local; not the HTTP envelope)."""


class SeedInputError(SeedAdminError):
    """Required seed email/password was not supplied via env or CLI."""


class AlreadyBootstrappedError(SeedAdminError):
    """An `admin_users` row already exists — the single-use guard refused."""


def _resolve_credentials(argv: list[str] | None = None) -> tuple[str, str]:
    """Resolve the seed email/password from CLI args (override) then env vars.

    Never hard-codes credentials. `--password` is documented as interactive-only; the production path
    is `SEED_ADMIN_PASSWORD` via secret injection.
    """
    parser = argparse.ArgumentParser(
        prog="seed_admin",
        description="One-time bootstrap of the initial super_admin (SP-03).",
    )
    parser.add_argument("--email", default=None, help="Seed admin email (overrides SEED_ADMIN_EMAIL).")
    parser.add_argument(
        "--password",
        default=None,
        help="Seed admin password (overrides SEED_ADMIN_PASSWORD; interactive use only).",
    )
    args = parser.parse_args(argv)

    email = args.email if args.email is not None else os.environ.get("SEED_ADMIN_EMAIL")
    password = args.password if args.password is not None else os.environ.get("SEED_ADMIN_PASSWORD")
    if not email or not password:
        raise SeedInputError(
            "SEED_ADMIN_EMAIL and SEED_ADMIN_PASSWORD (or --email/--password) are required"
        )
    return email, password


def _emit_bootstrap_audit(user_id: str, email: str) -> None:
    """Emit the `admin.bootstrap` audit line (actor=system). Never carries the seed password."""
    logger.info(
        json.dumps(
            {
                "action": "admin.bootstrap",
                "actor": "system",
                "user_id": user_id,
                "email": email,
                "role": "super_admin",
                "outcome": "success",
            }
        )
    )


async def bootstrap_super_admin(email: str, password: str, *, sessionmaker=None):
    """Create exactly one super-admin if none exists; else refuse. Async core (no FastAPI types).

    Raises `WeakPasswordError` (before any DB work) on a sub-policy password,
    `AlreadyBootstrappedError` when an `admin_users` row already exists, and propagates SQLAlchemy
    errors on a transient/DB failure (the caller maps these to a fail-closed exit). `sessionmaker`
    defaults to one built from `settings.database_url`; tests inject an aiosqlite one.
    """
    from sqlalchemy import select, text
    from sqlalchemy.exc import IntegrityError

    from modules.auth.models import AdminUser
    from modules.auth.password import hash_password, validate_password_policy

    # Validate policy up front so a too-weak seed is rejected before any DB work AND before hashing.
    validate_password_policy(password)

    normalized_email = email.strip().lower()
    password_hash = hash_password(password)

    owns_engine = sessionmaker is None
    engine = None
    if sessionmaker is None:
        from db import build_engine, build_sessionmaker

        from settings import Settings

        engine = build_engine(Settings().database_url)
        sessionmaker = build_sessionmaker(engine)

    try:
        async with sessionmaker() as session:
            try:
                # Advisory lock FIRST (Postgres only): serialize check-then-insert across connections
                # so a concurrent run cannot also pass the guard. aiosqlite has no such function —
                # offline tests are single-connection, and the real concurrency guarantee is covered
                # by the Postgres integration run (design D3). ponytail: one-line dialect guard.
                if session.bind is not None and session.bind.dialect.name == "postgresql":
                    await session.execute(
                        text("SELECT pg_advisory_xact_lock(:k)"), {"k": _BOOTSTRAP_LOCK_KEY}
                    )
                existing = await session.scalar(select(AdminUser.id).limit(1))
                if existing is not None:
                    await session.rollback()
                    raise AlreadyBootstrappedError()

                admin = AdminUser(
                    email=normalized_email,
                    password_hash=password_hash,
                    must_change_password=True,
                    active=True,
                )
                session.add(admin)
                await session.commit()
                _emit_bootstrap_audit(str(admin.id), admin.email)
                return admin
            except IntegrityError as exc:
                # Defence-in-depth backstop for a same-email race that slipped past the existence
                # check — roll back (releasing the lock) and report as a refusal.
                await session.rollback()
                raise AlreadyBootstrappedError() from exc
    finally:
        if owns_engine and engine is not None:
            await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Returns a process exit code; emits no password material."""
    from errors import WeakPasswordError

    logging.basicConfig(level=logging.INFO)

    try:
        email, password = _resolve_credentials(argv)
    except SeedInputError as exc:
        logger.error("seed_admin: %s", exc)
        return EXIT_REFUSED

    try:
        admin = asyncio.run(bootstrap_super_admin(email, password))
    except WeakPasswordError:
        logger.error(
            "seed_admin: seed password rejected — it does not meet the configured password policy; "
            "no admin created"
        )
        return EXIT_REFUSED
    except AlreadyBootstrappedError:
        logger.warning(
            "seed_admin: a super_admin already exists — bootstrap is single-use; no second admin "
            "created"
        )
        return EXIT_REFUSED
    except Exception:
        # Transient/DB failure → fail closed. Do not log the exception payload (it could echo input);
        # nothing was created.
        logger.error("seed_admin: bootstrap failed due to a database error; nothing was created")
        return EXIT_TRANSIENT

    logger.info(
        "seed_admin: bootstrapped initial super_admin %s (id=%s); rotate the seed password on first "
        "login",
        admin.email,
        admin.id,
    )
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess/CLI
    sys.exit(main())
