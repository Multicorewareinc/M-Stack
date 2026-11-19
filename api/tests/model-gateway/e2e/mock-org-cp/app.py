"""Minimal stand-in for Organization Control Plane's API-key verify endpoint.

The gateway authenticates every /v1 request by resolving the bearer's hash against Org CP
(`GET /internal/v1/api-keys/verify?hash=...`, ADR-026) and fails CLOSED on any transport or
non-200 error. The queue end-to-end stack is about the event backbone, not RBAC, so this
stubs that one route -- exactly the way mock-upstream stubs the model provider -- instead of
dragging a real Org CP plus its Postgres and seeding into the run.

Any hash verifies as the same valid, non-revoked, non-expiring key belonging to an active
owner, which is all `dependencies._is_valid` checks. The org_id it returns becomes the
gateway's `principal`, so it is the identity the rate limiters count and billing meters.
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

ORG_ID = "11111111-1111-1111-1111-111111111111"
KEY_ID = "22222222-2222-2222-2222-222222222222"
OWNER_ID = "33333333-3333-3333-3333-333333333333"


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/health":
            return self._json(200, {"status": "ok"})

        if parsed.path == "/internal/v1/api-keys/verify":
            key_hash = (parse_qs(parsed.query).get("hash") or [""])[0]
            if not key_hash:
                return self._json(404, {"detail": "unknown key"})
            # The shape dependencies._org_cp_verify/_is_valid expect.
            return self._json(200, {
                "id": KEY_ID,
                "org_id": ORG_ID,
                "owner_id": OWNER_ID,
                "revoked_at": None,
                "expires_at": None,
                "owner_active": True,
            })

        return self._json(404, {"detail": "not found"})

    def log_message(self, *_args):
        pass


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
