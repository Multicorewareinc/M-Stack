"""HTTP routes. Thin: parse, delegate to the service, wrap the result.

- /health, /metrics  -> no auth (ops)
- /v1/*              -> require the fixed API key

When an event publisher is configured (ADR-007), /v1/chat/completions and
/v1/completions also emit request/response events on the async strain via `_respond`
(the sync forwarding is unchanged; the emit is fire-and-forget). When no publisher is
configured, `_respond` is transparent — today's exact path.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable

import events
from dependencies import enforce_policies, verify_api_key
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from service import Result

from errors import AppError, BadRequestError

router = APIRouter()


@router.get("/health", tags=["ops"])
async def health() -> dict:
    return {"status": "ok"}


@router.get("/metrics", tags=["ops"])
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


v1 = APIRouter(prefix="/v1", dependencies=[Depends(verify_api_key), Depends(enforce_policies)])


def _to_response(result: Result):
    if result.stream is not None:
        return StreamingResponse(result.stream, status_code=result.status_code, media_type=result.media_type)
    return JSONResponse(status_code=result.status_code, content=result.body)


async def _payload(request: Request) -> dict:
    try:
        payload = await request.json()
    except ValueError as exc:
        raise BadRequestError("request body must be valid JSON") from exc
    if not isinstance(payload, dict) or not payload.get("model"):
        raise BadRequestError("'model' is required")
    return payload


def _usage(body: dict | None) -> dict | None:
    return body.get("usage") if isinstance(body, dict) else None


async def _respond(request: Request, payload: dict, result_coro: Awaitable[Result]):
    """Emit request/response events around the service call, then return the response.

    Transparent (today's exact path) when no publisher is configured. Otherwise: emit a
    `request` event before forwarding, and a `response` event on success AND error, so the
    audit trail is complete; streaming is tee'd (forward chunk first, copy after)."""
    pub: events.EventPublisher | None = request.app.state.event_publisher
    if pub is None:
        return _to_response(await result_coro)

    ctx = {
        "request_id": request.headers.get("X-Request-Id") or uuid.uuid4().hex,
        "principal": getattr(request.state, "principal", None),
        "api_key_id": getattr(request.state, "api_key_id", None),
        "owner_id": getattr(request.state, "owner_id", None),
        "model": payload.get("model"),
        "path": request.url.path,
    }
    capture = pub.capture_bodies
    start = time.monotonic()
    pub.emit(events.request_event(ctx, payload if capture else None))

    try:
        result = await result_coro
    except Exception as exc:
        status = exc.status_code if isinstance(exc, AppError) else 500
        pub.emit(events.response_event(
            ctx, status=status, usage=None,
            duration_ms=(time.monotonic() - start) * 1000, stream=False,
        ))
        raise  # the installed error handler builds the client body, unchanged

    if result.stream is None:
        pub.emit(events.response_event(
            ctx, status=result.status_code, usage=_usage(result.body),
            duration_ms=(time.monotonic() - start) * 1000, stream=False,
            body=result.body if capture else None,
        ))
        return JSONResponse(status_code=result.status_code, content=result.body)

    async def tee():
        seq = 0
        usage: dict | None = None
        terminal = False
        buf = b""
        try:
            async for chunk in result.stream:
                yield chunk  # client first — TTFT untouched
                pub.emit(events.chunk_event(ctx["request_id"], seq, chunk, capture))
                seq += 1
                buf, lines = events.split_complete_lines(buf + chunk)
                for line in lines:
                    usage = usage or events.usage_from_sse_line(line)
                    terminal = terminal or events.is_terminal(line)
            pub.emit(events.response_event(
                ctx, status=result.status_code, usage=usage,
                duration_ms=(time.monotonic() - start) * 1000, stream=True,
                partial=not terminal, chunks=seq,
            ))
        except BaseException:  # abort / client disconnect / upstream stream error
            pub.emit(events.response_event(
                ctx, status=result.status_code, usage=usage,
                duration_ms=(time.monotonic() - start) * 1000, stream=True,
                partial=True, chunks=seq,
            ))
            raise
        # NB: completion event fires exactly once — try-end OR except, never a finally.

    return StreamingResponse(tee(), status_code=result.status_code, media_type=result.media_type)


@v1.post("/chat/completions")
async def chat_completions(request: Request):
    payload = await _payload(request)
    return await _respond(
        request, payload,
        request.app.state.service.chat_completions(payload, stream=bool(payload.get("stream", False))),
    )


@v1.post("/completions")
async def completions(request: Request):
    payload = await _payload(request)
    return await _respond(
        request, payload,
        request.app.state.service.completions(payload, stream=bool(payload.get("stream", False))),
    )


@v1.get("/models")
async def list_models(request: Request):
    # Not emitted: no payload, no model/principal to bill or audit as a forwarded request.
    return _to_response(request.app.state.service.list_models())


router.include_router(v1)
