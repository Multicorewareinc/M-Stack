"""HTTP routes. Thin: parse, delegate to the service, wrap the result.

- POST /check         -> policy-plane contract (docs/policy-plane.md); no auth (internal)
- GET  /health        -> ops
- GET  /metrics       -> Prometheus (ops)
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest
from pydantic import BaseModel

router = APIRouter()

DECISIONS = Counter("rl_tpm_decisions_total", "TPM policy decisions", ["decision"])
CONSUMER_ACTIVE = Gauge("rl_tpm_consumer_active", "1 when the async counting consumer is bound, else 0")


def _sync_consumer_gauge(consumer) -> bool:
    """Reflect the consumer's live state on the gauge and return whether it is counting."""
    counting = consumer is not None and consumer.active
    CONSUMER_ACTIVE.set(1.0 if counting else 0.0)
    return counting


class CheckRequest(BaseModel):
    principal: str | None = None
    model: str | None = None
    path: str | None = None


@router.post("/check", tags=["policy"])
async def check(body: CheckRequest, request: Request) -> dict:
    # /check always returns HTTP 200; the decision lives in the body (the gateway
    # reads body.decision and ignores this status — docs/policy-plane.md, ADR-004).
    result = await request.app.state.service.check(body.principal, body.model, body.path)
    DECISIONS.labels(result["decision"]).inc()
    return result


@router.get("/health", tags=["ops"])
async def health() -> dict:
    # Liveness only: uvicorn is up. Stays unconditional — a restart can't clear a durable-bind
    # conflict, so liveness must not depend on counting state (ADR-016). Readiness is /ready.
    return {"status": "ok"}


@router.get("/ready", tags=["ops"])
async def ready(request: Request) -> Response:
    # Counting readiness (ADR-016). No consumer configured (EVENT_BACKBONE_URL empty) is the
    # deliberate inert path -> Ready. Configured-but-not-counting -> 503 so the fault can't hide.
    # ponytail: /ready exists for a future K8s readiness probe; compose still gates on /health.
    consumer = request.app.state.consumer
    counting = _sync_consumer_gauge(consumer)
    if consumer is not None and not counting:
        return JSONResponse({"status": "degraded", "counting": False}, status_code=503)
    return JSONResponse({"status": "ok", "counting": counting})


@router.get("/metrics", tags=["ops"])
async def metrics(request: Request) -> Response:
    _sync_consumer_gauge(request.app.state.consumer)
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
