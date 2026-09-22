"""HTTP routes. Ops-only: the enricher has no client-facing API.

- GET /health  -> liveness (uvicorn is up)
- GET /metrics -> Prometheus; the consumer-active gauge is the readiness signal (no /ready,
  ADR-016 notes it is unwired in the compose stack — ponytail).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, Gauge, generate_latest

router = APIRouter()

CONSUMER_ACTIVE = Gauge("enricher_consumer_active", "1 when the enricher consumer is bound, else 0")


def _sync_consumer_gauge(consumer) -> None:
    CONSUMER_ACTIVE.set(1.0 if (consumer is not None and consumer.active) else 0.0)


@router.get("/health", tags=["ops"])
async def health() -> dict:
    # Liveness only: a restart can't clear a durable-bind conflict, so liveness must not
    # depend on consumer state (ADR-016). The consumer-active gauge on /metrics is readiness.
    return {"status": "ok"}


@router.get("/metrics", tags=["ops"])
async def metrics(request: Request) -> Response:
    _sync_consumer_gauge(request.app.state.consumer)
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
