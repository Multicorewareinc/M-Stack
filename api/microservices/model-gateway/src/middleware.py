"""Generic X-RateLimit-* response-header stamping (ADR-012, docs/policy-plane.md).

Pure ASGI middleware (NOT Starlette BaseHTTPMiddleware, which buffers/breaks the gateway's
SSE streaming). It only appends headers on `http.response.start` from whatever the policy
chain stashed on `request.state.ratelimit` (== scope["state"]["ratelimit"]). Capability-
agnostic: it names no policy and is inert when nothing is stashed (no policies, or no policy
returned the optional limit/remaining/reset_after fields).
"""

from __future__ import annotations


class RateLimitHeaderMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                state = scope.get("state", {})
                rl = state.get("ratelimit")
                if rl:
                    headers = message.setdefault("headers", [])
                    headers.append((b"x-ratelimit-limit", str(rl["limit"]).encode()))
                    headers.append((b"x-ratelimit-remaining", str(rl["remaining"]).encode()))
                    headers.append((b"x-ratelimit-reset", str(rl["reset"]).encode()))
                # Per-scope (usage-showcase UI: distinct RPM vs TPM bars) — additive, alongside
                # the merged headers above, never replacing them.
                for name, rl_scope in (state.get("ratelimit_scopes") or {}).items():
                    headers = message.setdefault("headers", [])
                    headers.append((f"x-ratelimit-{name}-limit".encode(), str(rl_scope["limit"]).encode()))
                    headers.append((f"x-ratelimit-{name}-remaining".encode(), str(rl_scope["remaining"]).encode()))
                    headers.append((f"x-ratelimit-{name}-reset".encode(), str(rl_scope["reset"]).encode()))
            await send(message)

        await self.app(scope, receive, send_wrapper)
