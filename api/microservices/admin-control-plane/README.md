# admin-control-plane

The platform's **admin control plane** — the first DB-backed service in the repo. Super-admins
manage **Plans** (tpm/rpm/quota bundles, three seeded default tiers) and **Organizations**
(each attached to exactly one plan) over an admin Postgres database. Managed data only this
increment: plan limits are **not** yet projected into the live rate-limiters.

Layout deviates from the flat ADR-003 boilerplate — see **ADR-015** (per-module packages under
`src/modules/`, first Postgres + async SQLAlchemy + Alembic in the repo).

## Endpoints

All `/v1/*` routes require `Authorization: Bearer <ADMIN_API_KEY>`. `/health` and `/metrics`
are unauthenticated.

| Method | Path | Notes |
|---|---|---|
| POST | `/v1/plans` | create → 201 |
| GET | `/v1/plans` | list |
| GET | `/v1/plans/{id}` | 404 if absent |
| PATCH | `/v1/plans/{id}` | partial update |
| DELETE | `/v1/plans/{id}` | 204; **409** if referenced by an org |
| POST | `/v1/organizations` | create → 201; **422** if `plan_id` unknown |
| GET | `/v1/organizations` | list |
| GET | `/v1/organizations/{id}` | 404 if absent |
| PATCH | `/v1/organizations/{id}` | rename / change plan / change status |
| DELETE | `/v1/organizations/{id}` | 204 |
| GET | `/health` | `{"status":"ok"}` (no auth) |
| GET | `/metrics` | Prometheus text (no auth) |

Duplicate `name` → 409. Validation failures / unknown plan → 422.

## Environment

| Var | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | — | SQLAlchemy async URL, e.g. `postgresql+asyncpg://user:pass@host:5432/db` |
| `ADMIN_API_KEY` | — | super-admin bearer token (set this) |
| `LOG_LEVEL` | `INFO` | log level |
| `SEED_PLANS` | `true` | seed Free/Pro/Enterprise on startup (insert-missing only) |
| `BILLING_INTERNAL_URL` | `http://billing:8000` | billing's internal API — best-effort subscription notification on org creation/plan change (ADR-032) |
| `BILLING_TIMEOUT_MS` | `3000` | timeout for the billing notification call |

A plan's `stripe_price_id` (optional, settable via the same `POST`/`PATCH /v1/plans`) is
best-effort-forwarded to billing whenever an org is created with a plan or has its plan
changed — a billing outage never fails the org/plan operation (ADR-032); see
`add-billing-plan-subscription-linkage`.

## Run

```
# migrations own production DDL:
alembic upgrade head
uvicorn main:create_app --factory --host 0.0.0.0 --port 8000   # from src/
```

The Docker `runtime` image does both via `docker-entrypoint.sh` (`alembic upgrade head` then serve).

## Test (two runs — both must be green)

**Offline** (fast, aiosqlite injected, no Postgres):

```
# from api/tests/admin-control-plane/
python -m pytest -q
```

**Docker**:

```
# 1) offline suite in-container — tests live under api/tests/admin-control-plane/ and are
#    mounted at run (single source of truth); run from the repo root:
docker build --target test -t admin-cp-test api/microservices/admin-control-plane
docker run --rm -v "$(pwd)/api/tests/admin-control-plane/tests:/app/tests" admin-cp-test

# 2) namespaced Postgres integration (alembic upgrade head + asyncpg CRUD + real FK-RESTRICT)
#    see api/tests/admin-control-plane/integration/ and the scratchpad compose harness
```

Offline tests inject an in-memory aiosqlite engine (mirrors the `FakeRedis` injection idiom);
Postgres-specific behavior (the migration, the asyncpg driver, DB-enforced `ON DELETE RESTRICT`)
is covered only on the Docker integration run.
