"""HTTP RED metrics (rate, errors, duration) for /metrics.

Identical in every first-party service (each image ships only its own src/),
so the series share names across services and Prometheus tells them apart by
the `job`/`service` labels the ServiceMonitor adds -- one dashboard query
covers the whole platform.

Pure ASGI middleware, NOT Starlette BaseHTTPMiddleware, which buffers and
breaks streaming (the gateway's SSE): it only observes `http.response.start`
for the status and times the call until the app returns, i.e. until the last
body chunk has been sent.

`route` is the matched route *template* (`/v1/orgs/{org_id}`), never the raw
path -- a path label would make one series per id. Requests no route matched
are folded into `<unmatched>` for the same reason.
"""

from __future__ import annotations

import time

from prometheus_client import Counter, Gauge, Histogram
from starlette.routing import Match

REQUESTS = Counter(
    "http_requests_total", "HTTP requests handled", ["method", "route", "status"],
)
DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration, until the last response byte is sent",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
)
IN_FLIGHT = Gauge("http_requests_in_flight", "HTTP requests currently being handled")

# Scraped every few seconds; counting them would only add noise to the RED view.
_SKIP = frozenset({"/metrics", "/health"})
UNMATCHED = "<unmatched>"


def _route_template(scope) -> str:
    route = scope.get("route")
    if route is not None and getattr(route, "path", None):
        return route.path
    # Older Starlette does not put the matched route on the scope.
    app = scope.get("app")
    for candidate in getattr(getattr(app, "router", None), "routes", ()):
        match, _ = candidate.matches(scope)
        if match == Match.FULL:
            return getattr(candidate, "path", UNMATCHED)
    return UNMATCHED


class HttpMetricsMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") in _SKIP:
            await self.app(scope, receive, send)
            return

        status = 500  # an exception before http.response.start is a 500
        start = time.perf_counter()

        async def send_wrapper(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        IN_FLIGHT.inc()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            IN_FLIGHT.dec()
            route = _route_template(scope)
            method = scope.get("method", "")
            REQUESTS.labels(method, route, str(status)).inc()
            DURATION.labels(method, route).observe(time.perf_counter() - start)
