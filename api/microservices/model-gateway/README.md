# model-gateway

Self-contained microservice: an **OpenAI-compatible reverse proxy** in front of an
inference server (vLLM, TGI, Ollama, OpenAI, or any OpenAI-compatible endpoint).
Everything needed to build and run the container lives in this folder.

## Layout

```
model-gateway/
├── Dockerfile
├── requirements.txt
├── .env.example
├── README.md
└── src/
    ├── main.py          # app factory, logging, wiring
    ├── router.py        # HTTP routes (/v1/*, /health, /metrics)
    ├── service.py       # gateway logic: pick upstream + forward (streaming)
    ├── dependencies.py  # fixed API-key auth
    ├── settings.py      # env-driven config
    └── errors.py        # error types + OpenAI-shaped error responses
```

## Endpoints

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/v1/chat/completions` | Bearer | `"stream": true` streams SSE through |
| POST | `/v1/completions` | Bearer | legacy completions |
| GET  | `/v1/models` | Bearer | lists configured `MODEL_ROUTES` |
| GET  | `/health` | none | `{"status": "ok"}` |
| GET  | `/metrics` | none | Prometheus |

All `/v1/*` requests need `Authorization: Bearer <API_KEY>`. A missing/invalid key
returns `401` and is not forwarded.

## Configuration (env)

| Var | Default | Meaning |
|---|---|---|
| `API_KEY` | `pf-local-dev-key` | The fixed key `/v1/*` validates |
| `LOG_LEVEL` | `INFO` | Log level |
| `UPSTREAM_URL` | `http://localhost:8000` | Default upstream inference server |
| `UPSTREAM_API_KEY` | — | Bearer token sent to the upstream, if it needs one |
| `MODEL_ROUTES` | `{}` | JSON `{"model": "http://upstream"}`; unlisted models use `UPSTREAM_URL` |
| `REQUEST_TIMEOUT` | `60` | Upstream request timeout (seconds) |
| `POLICY_ENDPOINTS` | `` | Comma-separated policy-service URLs (`POST /check`); empty = inert. See [policy chain](#policy-chain-plug-and-play) |
| `POLICY_TIMEOUT_MS` | `50` | Per-policy call timeout |
| `POLICY_FAIL_MODE` | `closed` | `closed` = block if a policy errors; `open` = allow through |

## Policy chain (plug-and-play)

Pre-request policies (rate limit, quota, geo, content, …) attach **by config, not code**. Set `POLICY_ENDPOINTS` to one or more policy services; each implements `POST /check` and returns allow/deny. The gateway calls them all in parallel before forwarding; any `deny` rejects the request. Empty `POLICY_ENDPOINTS` (default) makes the chain inert — identical behavior to having no chain. See [docs/policy-plane.md](../docs/policy-plane.md).

```
POST /check  {"principal","model","path"}
  -> {"decision":"allow"}
   | {"decision":"deny","status":429,"type":"rate_limit_exceeded","retry_after":12,"reason":"..."}
```

## Run with Docker

```bash
docker build -t model-gateway .
docker run --rm -p 8080:8080 \
  -e API_KEY=my-secret \
  -e UPSTREAM_URL=http://host.docker.internal:8000 \
  model-gateway
```

## Run locally

```bash
pip install -r requirements.txt
cd src
uvicorn main:create_app --factory --host 0.0.0.0 --port 8080
```

## Try it

```bash
curl -s localhost:8080/health

curl -s localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer pf-local-dev-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"llama-3-8b","messages":[{"role":"user","content":"hi"}]}'
```
