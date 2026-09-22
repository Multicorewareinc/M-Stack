# tokenizer

A standalone token-counting microservice (ADR-014). Built as the fallback text-source for
`rate-limiter-tpm` (TF-01/ADR-011) when the model-gateway's `usage` field is absent, but
shaped as a general-purpose endpoint so other consumers (billing, analytics) can reuse it
without duplicating tokenizer logic. Everything to build and run the container lives in
this folder (ADR-003).

## Layout

```
tokenizer/
├── Dockerfile
├── requirements.txt
├── .env.example
├── README.md
└── src/
    ├── main.py       # app factory, JSON logging, registry wiring, startup validation
    ├── router.py     # routes: POST /tokenize, GET /health, GET /metrics
    ├── service.py    # name-keyed tokenizer registry (one backend wired: cl100k_base)
    ├── settings.py   # env-driven config (TOKENIZER_DEFAULT)
    └── errors.py     # error types + handlers (UnknownTokenizerError -> 400)
```

There is no `dependencies.py` (`/tokenize` has no auth — internal, network-trusted) and no
`models/`/`schemas/` (no persistence).

## The contract

```
POST /tokenize  {"text": "<string>", "tokenizer": "<optional, default TOKENIZER_DEFAULT>"}
  -> {"tokens": <int>, "tokenizer": "<name used>"}
   | 400 {"error": {"message": "...", "type": "unknown_tokenizer"}}
```

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/tokenize` | none | count tokens in text |
| GET  | `/health` | none | `{"status":"ok"}` |
| GET  | `/metrics` | none | Prometheus (`tokenizer_requests_total`) |

## Configuration (env)

| Var | Default | Meaning |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Log level |
| `TOKENIZER_DEFAULT` | `cl100k_base` | Registry key used when a request omits `tokenizer`. Must name a registered backend or the service fails to start. |

## Backend registry (ADR-014)

Exactly one backend is wired in this build: `cl100k_base`, via `tiktoken`
(`src/service.py::TOKENIZERS`). Adding a second backend later is additive — a new dict
entry — with no change to the `/tokenize` contract. An explicitly named but unregistered
`tokenizer` value is rejected (`400 unknown_tokenizer`), never silently substituted for the
default.

## Run with Docker

```bash
docker build -t tokenizer .
docker run --rm -p 8000:8000 tokenizer
```

## Run locally

```bash
pip install -r requirements.txt
cd src
uvicorn main:create_app --factory --host 0.0.0.0 --port 8000
```

## Test

```bash
# offline (injected fake tokenizer registry — no tiktoken/network needed)
pytest -q

# inside the container (test stage) — tests live under api/tests/tokenizer/ and are mounted
# at run (single source of truth); run from this service dir:
docker build --target test -t tokenizer-test .
docker run --rm -v "$(pwd)/../../tests/tokenizer/tests:/app/tests" tokenizer-test
```

Tests live only under `api/tests/tokenizer/` (project.md). The `test` image generates its own
container `pytest.ini` (`pythonpath = src`); the canonical tests are bind-mounted at run, so
there is no second copy in this folder to keep in sync. The offline run from
`api/tests/tokenizer/` uses that folder's `pytest.ini` (`pythonpath =
../../microservices/tokenizer/src`), the two-tier layout `docs/testing-strategy.md` documents.
