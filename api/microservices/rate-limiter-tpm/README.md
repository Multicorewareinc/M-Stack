# rate-limiter-tpm

A standalone **policy-plane** microservice: the second plug for the model-gateway's
pluggable policy chain (`docs/policy-plane.md`, ADR-004), alongside `rate-limiter-rpm`. It
enforces **tokens-per-minute** limits — per-user, per-model, and per-user+model — over a
fixed 60s window in shared Valkey, and answers the generic `POST /check` contract. It
attaches to the gateway **by config only** (`POLICY_ENDPOINTS`). Everything to build and
run the container lives in this folder (ADR-003).

## Layout

```
rate-limiter-tpm/
├── Dockerfile
├── requirements.txt
├── .env.example
├── README.md
└── src/
    ├── main.py       # app factory, JSON logging, redis wiring, startup config validation
    ├── router.py     # routes: POST /check, GET /health, GET /ready, GET /metrics
    ├── service.py    # TPM enforcement (check/count)
    ├── settings.py   # env-driven config (VALKEY_URL, RATE_LIMITS, ADMIN_CP_URL, EVENT_STREAM_*, ...)
    ├── plan_client.py # resolves an org's plan tpm limit from admin-control-plane, cached
    ├── errors.py     # generic error handlers
    └── consumer.py   # NATS consumer: counts tokens from the enriched response events
```

There is no `dependencies.py` (`/check` has no auth — internal, network-trusted) and no
`models/`/`schemas/` (no persistence beyond Valkey counters).

## The contract (docs/policy-plane.md)

```
POST /check  {"principal": <id|null>, "model": <name|null>, "path": <route>}
  -> {"decision":"allow"}
   | {"decision":"deny","status":429,"type":"rate_limit_exceeded",
      "retry_after":<seconds-to-window-reset>,"reason":"<scope> tpm exceeded"}
```

Identical shape to `rate-limiter-rpm`'s `/check` — only the `reason` text and the *units*
being counted (tokens, not requests) differ. `/check` always returns **HTTP 200**; the
decision lives in the body (ADR-004).

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/check` | none | policy-plane check |
| GET  | `/health` | none | `{"status":"ok"}` |
| GET  | `/metrics` | none | Prometheus (`rl_tpm_decisions_total{decision}`) |

## Configuration (env)

| Var | Default | Meaning |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Log level |
| `VALKEY_URL` | `redis://localhost:6379/0` | **Same Valkey instance as `rate-limiter-rpm`** (ADR-011), prefix `rl:tpm:` |
| `RATE_LIMITS` | `{}` | JSON limits, same shape as RPM's — values are **token** counts |
| `EVENT_BACKBONE_URL` | `""` | NATS JetStream URL; empty = no consumer, counters never advance |
| `EVENT_STREAM_NAME` | `GATEWAY_EVENTS_ENRICHED` | Must equal this exactly when a backbone is configured (ADR-030 AD-07) |
| `EVENT_STREAM_SUBJECT` | `gateway.events.enriched` | Must equal this exactly when a backbone is configured (ADR-030 AD-07) |

## How counting works (AD-01/AD-02, ADR-030)

Unlike RPM (which counts `request` events), this service's NATS consumer subscribes to the
gateway's **enriched** `response` events on `gateway.events.enriched` — produced by the
`enricher` service (ADR-030), which guarantees every event carries a usable token count:

1. **`usage.total_tokens` present as a number** — the only path this service has: `INCR`
   the applicable scope counters by that amount. No tokenizing, no HTTP call of any kind;
   this service has no tokenizer dependency at all (that fallback lives entirely in
   `enricher` now, ADR-030 AD-02, superseding ADR-011 AD-06's in-service fallback).
2. **`usage.total_tokens` is `null`, absent, or non-numeric** (the enricher found no
   reliable count, `usage.source == "none"`): skip the event. No count, no error, never a
   guess.

This service does not distinguish `usage.source == "provider"` from `"estimated"` — both
count identically; that provenance is a billing concern (see `billing`, a separate
service), not a rate-limiting one.

Because counting is asynchronous, TPM is a **soft limit** — a burst can slightly overshoot
before it's enforced, same as RPM.

## Startup validation (ADR-030 AD-07)

When `EVENT_BACKBONE_URL` is set, this service validates at startup — **before** binding
any consumer — that `EVENT_STREAM_SUBJECT` and `EVENT_STREAM_NAME` are exactly the enriched
pair shown above. This is an **allowlist** check (must equal the enriched pair), not a
blocklist (must not equal the one known-bad raw value): a typo'd subject or a mismatched
subject/stream-name pairing fails the same way the raw `gateway.events` value does. On
failure the container **does not start** at all (`RuntimeError`, not a 5xx response) — a
new, distinct failure mode from the existing "configured but inactive" `503 /ready` state,
which is a *running* service that simply hasn't bound its consumer yet.

## Attach to the gateway (config only)

```bash
# on the model-gateway container (comma-separated alongside rate-limiter-rpm)
POLICY_ENDPOINTS=http://rate-limiter-rpm:8000/check,http://rate-limiter-tpm:8000/check

# on the rate-limiter-tpm container — EVENT_BACKBONE_URL requires enricher to be running
# and consuming gateway.events (ADR-030 AD-07); this service has no tokenizer dependency
VALKEY_URL=redis://valkey:6379/0        # same instance as rate-limiter-rpm
EVENT_BACKBONE_URL=nats://nats:4222
RATE_LIMITS={"user_default":60000,"model_overrides":{"gpt-4o":120000}}
```

**Ops note:** the previous durable (`tpm-counter` on the raw `GATEWAY_EVENTS` stream)
should be retained for a minimum rollback window after cutover before any out-of-band
deletion — a rollback to a prior image depends on it still existing.

## Run with Docker

```bash
docker build -t rate-limiter-tpm .
docker run --rm -p 8000:8000 \
  -e VALKEY_URL=redis://host.docker.internal:6379/0 \
  -e RATE_LIMITS='{"user_default":60000}' \
  rate-limiter-tpm
```

## Run locally

```bash
pip install -r requirements.txt
cd src
uvicorn main:create_app --factory --host 0.0.0.0 --port 8000
```

## Test

```bash
# offline (injected in-memory fake Valkey; no tokenizer dependency to stub)
pytest -q

# inside the container (test stage) — tests live under api/tests/rate-limiter-tpm/ and are
# mounted at run (single source of truth); run from this service dir:
docker build --target test -t rate-limiter-tpm-test .
docker run --rm -v "$(pwd)/../../tests/rate-limiter-tpm/tests:/app/tests" rate-limiter-tpm-test
```

Tests live only under `api/tests/rate-limiter-tpm/` (project.md). The `test` image generates
its own container `pytest.ini` (`pythonpath = src`) and the canonical tests are bind-mounted
at run, so there is no second copy in this folder to keep in sync.
