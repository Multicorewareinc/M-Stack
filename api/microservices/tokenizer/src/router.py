"""HTTP routes. Thin: parse, delegate to the service, wrap the result.

- POST /tokenize      -> count tokens in text using a named/default backend; no auth (internal)
- GET  /health        -> ops
- GET  /metrics       -> Prometheus (ops)
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest
from pydantic import BaseModel

router = APIRouter()

REQUESTS = Counter("tokenizer_requests_total", "Tokenize requests")


class TokenizeRequest(BaseModel):
    text: str
    tokenizer: str | None = None


@router.post("/tokenize", tags=["tokenizer"])
async def tokenize(body: TokenizeRequest, request: Request) -> dict:
    tokens, name = request.app.state.service.count(body.text, body.tokenizer)
    REQUESTS.inc()
    return {"tokens": tokens, "tokenizer": name}


@router.get("/health", tags=["ops"])
async def health() -> dict:
    return {"status": "ok"}


@router.get("/metrics", tags=["ops"])
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
