"""Gateway logic: resolve the upstream for a request and forward it, streaming the
response through. OpenAI upstreams are pure passthrough; Anthropic upstreams are
translated via adapters.py. httpx lives here (nowhere else).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass

import adapters
import httpx

from errors import BadGatewayError, BadRequestError, ServiceUnavailableError
from settings import Settings


@dataclass
class Result:
    """Framework-agnostic response. Exactly one of `body` or `stream` is set."""

    status_code: int
    body: dict | None = None
    stream: AsyncIterator[bytes] | None = None
    media_type: str = "application/json"


@dataclass(frozen=True)
class Target:
    url: str
    type: str = "openai"  # "openai" | "anthropic"
    api_key: str | None = None


class GatewayService:
    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self._s = settings
        self._routes = settings.routes()
        self._client = client or httpx.AsyncClient(timeout=settings.request_timeout)

    # --- routing -----------------------------------------------------------
    def _target(self, model: str | None) -> Target:
        entry = self._routes.get(model or "")
        if entry is None:
            return Target(self._s.upstream_url.rstrip("/"), "openai", self._s.upstream_api_key)
        if isinstance(entry, str):
            return Target(entry.rstrip("/"), "openai", None)
        return Target(entry["url"].rstrip("/"), entry.get("type", "openai"), entry.get("api_key"))

    def _headers(self, target: Target) -> dict[str, str]:
        if target.type == "anthropic":
            headers = {"anthropic-version": adapters.ANTHROPIC_VERSION}
            if target.api_key:
                headers["x-api-key"] = target.api_key
            return headers
        return {"Authorization": f"Bearer {target.api_key}"} if target.api_key else {}

    # --- endpoints ---------------------------------------------------------
    async def chat_completions(self, payload: dict, *, stream: bool) -> Result:
        target = self._target(payload.get("model"))
        if target.type == "anthropic":
            return await self._anthropic_chat(target, payload, stream=stream)
        return await self._openai_forward(target, "/v1/chat/completions", payload, stream=stream)

    async def completions(self, payload: dict, *, stream: bool) -> Result:
        target = self._target(payload.get("model"))
        if target.type == "anthropic":
            raise BadRequestError("legacy /v1/completions is not supported by an anthropic upstream")
        return await self._openai_forward(target, "/v1/completions", payload, stream=stream)

    def list_models(self) -> Result:
        data = [{"id": m, "object": "model", "owned_by": "gateway"} for m in self._routes]
        return Result(status_code=200, body={"object": "list", "data": data})

    # --- forwarders --------------------------------------------------------
    async def _openai_forward(self, target: Target, path: str, payload: dict, *, stream: bool) -> Result:
        url, headers = target.url + path, self._headers(target)
        if not stream:
            resp = await self._send(lambda: self._client.post(url, json=payload, headers=headers))
            return _buffered(resp.status_code, resp.content)
        return await self._stream(url, payload, headers)

    async def _anthropic_chat(self, target: Target, payload: dict, *, stream: bool) -> Result:
        body = adapters.to_anthropic_request(payload)
        url, headers, model = target.url + "/v1/messages", self._headers(target), payload["model"]
        if not stream:
            resp = await self._send(lambda: self._client.post(url, json=body, headers=headers))
            if resp.status_code >= 400:
                return _buffered(resp.status_code, resp.content)
            return Result(status_code=200, body=adapters.to_openai_response(resp.json(), model=model))

        req = self._client.build_request("POST", url, json=body, headers=headers)
        resp = await self._send(lambda: self._client.send(req, stream=True))
        if resp.status_code >= 400:
            content = await resp.aread()
            await resp.aclose()
            return _buffered(resp.status_code, content)

        async def gen() -> AsyncIterator[bytes]:
            try:
                async for chunk in adapters.translate_stream(resp.aiter_lines(), model=model):
                    yield chunk
            finally:
                await resp.aclose()

        return Result(status_code=200, stream=gen(), media_type="text/event-stream")

    async def _stream(self, url: str, payload: dict, headers: dict) -> Result:
        payload = _with_usage_requested(payload)
        req = self._client.build_request("POST", url, json=payload, headers=headers)
        resp = await self._send(lambda: self._client.send(req, stream=True))
        if resp.status_code >= 400:
            content = await resp.aread()
            await resp.aclose()
            return _buffered(resp.status_code, content)

        async def gen() -> AsyncIterator[bytes]:
            try:
                async for chunk in resp.aiter_raw():
                    yield chunk
            finally:
                await resp.aclose()

        return Result(status_code=resp.status_code, stream=gen(), media_type="text/event-stream")

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    async def _send(call):
        try:
            return await call()
        except httpx.TimeoutException as exc:
            raise ServiceUnavailableError("upstream timed out") from exc
        except httpx.RequestError as exc:
            raise ServiceUnavailableError("upstream unreachable") from exc


def _with_usage_requested(payload: dict) -> dict:
    """An OpenAI-compatible streaming upstream only emits a trailing `data:` line
    carrying `usage` when asked (`stream_options.include_usage=true`) -- without it
    there is no usage anywhere in the stream for router.py's own `usage_from_sse_line`
    (ADR-007/030) to find, and every streamed response goes uncounted no matter how the
    event backbone or enricher are configured. Never overrides an explicit
    `stream_options` the caller already set -- a client asking for `include_usage:
    false` deliberately is respected, not silently reversed."""
    if "stream_options" in payload:
        return payload
    return {**payload, "stream_options": {"include_usage": True}}


def _buffered(status_code: int, content: bytes) -> Result:
    try:
        body = json.loads(content) if content else {}
    except ValueError as exc:
        raise BadGatewayError("upstream returned a non-JSON response") from exc
    return Result(status_code=status_code, body=body)
