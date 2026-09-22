"""HTTP routes. Thin: parse the body, delegate to the service, wrap the
result. No business logic, no I/O here.

- GET  /health                             -> ops, no auth
- GET  /metrics                            -> Prometheus, no auth
- POST /v1/sessions                        -> start a session
- POST /v1/sessions/{id}/messages          -> continue a session
- GET  /v1/sessions/{id}                   -> read-only status check
- POST /v1/sessions/stream                 -> start a session, streamed
- POST /v1/sessions/{id}/messages/stream   -> continue a session, streamed

The two /stream routes do the same work as their blocking counterparts
but emit newline-delimited JSON as the turn runs (one object per line),
so a UI can show which tool is being called while a multi-second,
possibly multi-turn model call is still in flight. The last line of any
stream is always {"type": "final", ...} carrying the same fields the
blocking endpoints return, so a client can treat it identically once it
arrives. NDJSON rather than SSE because the browser calls these with
fetch() + a POST body and a custom auth header, neither of which
EventSource supports.
"""

from __future__ import annotations

import uuid
from typing import Iterator, Literal

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from dependencies import require_api_key

router = APIRouter()


class MessageRequest(BaseModel):
    """Body for starting or continuing a conversation."""

    message: str


class SessionResponse(BaseModel):
    """The one response shape every session endpoint returns. `status`
    tells the caller what to do next: "needs_input" -> send another
    message, "resolved" -> a script was generated, read `script`."""

    session_id: str
    status: Literal["needs_input", "resolved"]
    message: str | None = None  # the model's question or final reply text
    script: str | None = None  # the generated script, once status == "resolved"


def _to_response(session_id: str, state: dict) -> SessionResponse:
    """Maps a graph state dict -- fresh from send_message() or a read-only
    get_state() snapshot, both carry the same reply/script shape -- onto
    SessionResponse. "resolved" means a script was produced during this
    conversation; "needs_input" means the model is still asking or has
    nothing to hand back yet."""
    script = state.get("script")
    return SessionResponse(
        session_id=session_id,
        status="resolved" if script else "needs_input",
        message=state.get("reply"),
        script=script,
    )


@router.get("/health", tags=["ops"])
async def health() -> dict:
    return {"status": "ok"}


@router.get("/metrics", tags=["ops"])
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.post(
    "/v1/sessions",
    response_model=SessionResponse,
    dependencies=[Depends(require_api_key)],
    tags=["sessions"],
)
def start_session(body: MessageRequest, request: Request) -> SessionResponse:
    """Starts a new conversation. Returns a session_id -- use it for
    every subsequent call for this conversation.

    Plain `def`, as are the other two routes that call the service
    directly: the model call is synchronous and can run for a minute, and
    FastAPI runs a `def` route in its thread pool but an `async def` one on
    the event loop, where it stalled every other request -- /health took
    53s behind a single turn. The /stream routes return a lazy generator,
    which Starlette already iterates in a thread."""
    session_id = uuid.uuid4().hex
    state = request.app.state.service.start_session(session_id, body.message)
    return _to_response(session_id, state)


@router.post(
    "/v1/sessions/{session_id}/messages",
    response_model=SessionResponse,
    dependencies=[Depends(require_api_key)],
    tags=["sessions"],
)
def continue_session(session_id: str, body: MessageRequest, request: Request) -> SessionResponse:
    """Sends another message on an existing conversation."""
    state = request.app.state.service.continue_session(session_id, body.message)
    return _to_response(session_id, state)


@router.get(
    "/v1/sessions/{session_id}",
    response_model=SessionResponse,
    dependencies=[Depends(require_api_key)],
    tags=["sessions"],
)
def get_session(session_id: str, request: Request) -> SessionResponse:
    """Read-only status check -- what state this session is currently in,
    without sending a new message."""
    state = request.app.state.service.get_session(session_id)
    return _to_response(session_id, state)


def _ndjson(session_id: str, events) -> Iterator[str]:
    """Renders graph events as newline-delimited JSON.

    The "final" event carries the whole graph state, which includes the
    full LangChain message history -- far more than a caller needs and
    not JSON-serializable anyway -- so it is mapped through the same
    _to_response() the blocking endpoints use. Everything else passes
    through as-is.
    """
    for event in events:
        if event.get("type") == "final":
            payload = _to_response(session_id, event).model_dump()
            payload["type"] = "final"
        else:
            payload = event
        yield json.dumps(payload) + "\n"


@router.post(
    "/v1/sessions/stream",
    dependencies=[Depends(require_api_key)],
    tags=["sessions"],
)
async def start_session_streaming(body: MessageRequest, request: Request) -> StreamingResponse:
    """Starts a new conversation, streaming progress as it happens."""
    session_id = uuid.uuid4().hex
    events = request.app.state.service.stream_session(session_id, body.message)
    return StreamingResponse(
        _ndjson(session_id, events),
        media_type="application/x-ndjson",
        # The dev proxy and any reverse proxy in front of this must not
        # sit on the response waiting for it to finish -- that would
        # defeat the entire point of streaming it.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post(
    "/v1/sessions/{session_id}/messages/stream",
    dependencies=[Depends(require_api_key)],
    tags=["sessions"],
)
async def continue_session_streaming(
    session_id: str, body: MessageRequest, request: Request
) -> StreamingResponse:
    """Sends another message on an existing conversation, streamed."""
    events = request.app.state.service.stream_session(session_id, body.message)
    return StreamingResponse(
        _ndjson(session_id, events),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
