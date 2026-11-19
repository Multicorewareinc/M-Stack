# Organization Control Plane (Org CP)

The organization-level control plane (ADR-020). Owns organization-level operational and RBAC
data as logical multi-tenant tenants in one org Postgres (one Org CP + one Org PG for all
organizations — not one per org). This first increment (SP-02) is the **service foundation**:
the tenant `organizations` record, the internal init/read endpoints Admin CP calls, the
fixed-bearer + tenant-context auth seam, and the reused persistence stack. Users, Roles, and
RBAC land in later increments.

Structure mirrors `admin-control-plane` (ADR-015): flat `src/` wiring + per-module packages
under `src/modules/`, Alembic `migrations/`, tests under `api/tests/organization-control-plane/`.

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/health` | none | liveness |
| GET | `/metrics` | none | Prometheus text |
| POST | `/internal/v1/organizations` | service bearer | init tenant row (idempotent on id) |
| GET | `/internal/v1/organizations/{id}` | service bearer | read tenant record |

## Config

| Var | Default | Meaning |
|---|---|---|
| `DATABASE_URL_ORG` | — | SQLAlchemy async URL, e.g. `postgresql+asyncpg://user:pass@host:5432/db` |
| `SERVICE_API_KEY` | — | service bearer (set this); shared with the calling control plane for now |
| `LOG_LEVEL` | `INFO` | log level |

## Invitations: email

An admin invites someone by email; the invitee opens a one-time link, chooses a password, and signs in.
The link is always shown to the admin to copy when the email could not be sent, so email is not required for
invitations to work.

**Emailing the invitation (SMTP).** Set these on the Org CP (Helm: `config.smtp.*`, secret `SMTP_PASSWORD`):

| Var | Default | Meaning |
|---|---|---|
| `SMTP_HOST` | — | mail server. Unset = no email |
| `SMTP_PORT` | `587` | |
| `SMTP_SECURITY` | `starttls` | `starttls` (587), `ssl` (465), or `none` (local relay / test mailbox only) |
| `SMTP_USERNAME`, `SMTP_PASSWORD` | — | optional login; the password belongs in a Secret, never a ConfigMap |
| `SMTP_FROM_ADDRESS` | — | sender address (required with `SMTP_HOST`) |
| `SMTP_FROM_NAME` | `Multistack` | sender display name |
| `ORG_PORTAL_BASE_URL` | — | public URL of the Organization Portal. The email carries an absolute link, so this is required too |

A send that fails (bad credentials, server down, ...) never fails the invitation: the response reports
`email_status` (`sent` / `failed` / `rejected` / `not_configured`) and the portals tell the admin to send the link themselves.

## Run

```
uvicorn main:create_app --factory --host 0.0.0.0 --port 8000
```

The Docker `runtime` image applies `alembic upgrade head` then serves (`docker-entrypoint.sh`).

## Test (two runs — both must be green)

**Offline** (fast, aiosqlite injected, no Postgres):

```
# from api/tests/organization-control-plane/
python -m pytest -q
```

**Docker**:

```
docker build --target test -t org-cp-test api/microservices/organization-control-plane
docker run --rm -v "$(pwd)/api/tests/organization-control-plane/tests:/app/tests" org-cp-test
# then the namespaced Postgres integration run (see integration/ + the scratchpad compose harness)
```

Offline tests inject an in-memory aiosqlite engine; Postgres-specific behaviour (the migration,
the asyncpg driver) is covered only on the Docker integration run (ADR-015a/ADR-020).
