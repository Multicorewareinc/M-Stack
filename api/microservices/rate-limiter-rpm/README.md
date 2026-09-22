# rate-limiter-rpm

A standalone **policy-plane** microservice: the first plug for the model-gateway's
pluggable policy chain (`docs/policy-plane.md`, ADR-004). It enforces
**requests-per-minute** limits — per-user, per-model, and per-user+model — over a
fixed 60s window in shared Valkey, and answers the generic `POST /check` contract.
It attaches to the gateway **by config only** (`POLICY_ENDPOINTS`) — no gateway code
changes. Everything to build and run the container lives in this folder (ADR-003).

## Layout

```
rate-limiter-rpm/
├── Dockerfile
├── requirements.txt
├── .env.example
├── README.md
└── src/
    ├── main.py       # app factory, JSON logging, redis wiring
    ├── router.py     # routes: POST /check, GET /health, GET /metrics
    ├── service.py    # RPM enforcement over the fixed 60s Valkey window
    ├── settings.py   # env-driven config (VALKEY_URL, RATE_LIMITS)
    └── errors.py     # generic error handlers
```

There is no `dependencies.py` (`/check` has no auth — internal, network-trusted) and no
`models/`/`schemas/` (no persistence beyond Valkey counters).

## The contract (docs/policy-plane.md)

```
POST /check  {"principal": <id|null>, "model": <name|null>, "path": <route>}
  -> {"decision":"allow"}
   | {"decision":"deny","status":429,"type":"rate_limit_exceeded",
      "retry_after":<seconds-to-window-reset>,"reason":"<scope> rpm exceeded"}
```

`/check` always returns **HTTP 200**; the decision lives in the body (the gateway reads
`decision` and ignores the /check status — ADR-004). It enforces **all applicable scopes**
and denies if **any** exceeds its limit within the current epoch-minute.

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/check` | none | policy-plane check |
| GET  | `/health` | none | `{"status":"ok"}` |
| GET  | `/metrics` | none | Prometheus (`rl_rpm_decisions_total{decision}`) |

## Configuration (env)

| Var | Default | Meaning |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Log level |
| `VALKEY_URL` | `redis://localhost:6379/0` | Shared Redis holding the counters (prefix `rl:rpm:`) |
| `RATE_LIMITS` | `{}` | JSON limits (below); empty ⇒ everything unlimited |

`RATE_LIMITS` shape (any field absent or `0` ⇒ that scope is unlimited / skipped):

```json
{
  "user_default": 60,
  "model_default": 0,
  "user_overrides": {"vip-key": 600},
  "model_overrides": {"gpt-4o": 120},
  "user_model_overrides": {"vip-key|gpt-4o": 300}
}
```

Limit resolution: `user_overrides[principal] ?? user_default`,
`model_overrides[model] ?? model_default`, and `user_model_overrides["<principal>|<model>"]`
(there is no user+model default). `retry_after` is the seconds remaining to the epoch-minute
boundary. On a Valkey error the service **fails open** (allows) so its own Valkey fault never
blocks gateway traffic.

## Attach to the gateway (config only)

Point the gateway's policy chain at this service and give the limiter its limits — no
gateway code changes:

```bash
# on the model-gateway container
POLICY_ENDPOINTS=http://rate-limiter-rpm:8000/check
# on the rate-limiter-rpm container
VALKEY_URL=redis://redis:6379/0
RATE_LIMITS={"user_default":60,"model_overrides":{"gpt-4o":120}}
```

## Run with Docker

```bash
docker build -t rate-limiter-rpm .
docker run --rm -p 8000:8000 \
  -e VALKEY_URL=redis://host.docker.internal:6379/0 \
  -e RATE_LIMITS='{"user_default":60}' \
  rate-limiter-rpm
```

## Run locally

```bash
pip install -r requirements.txt
cd src
uvicorn main:create_app --factory --host 0.0.0.0 --port 8000
```

## Test

```bash
# offline (injected in-memory fake Valkey — no real Valkey needed)
pytest -q

# inside the container (test stage) — tests live under api/tests/rate-limiter-rpm/ and are
# mounted at run (single source of truth); run from this service dir:
docker build --target test -t rate-limiter-rpm-test .
docker run --rm -v "$(pwd)/../../tests/rate-limiter-rpm/tests:/app/tests" rate-limiter-rpm-test
```
