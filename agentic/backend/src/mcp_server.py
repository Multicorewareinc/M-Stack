"""
MCP tool layer -- exposes MultiStack SDK resources as MCP tools.

Per the architecture decision (see docs/architecture-decision.md): the
model calls one tool per resource action, and each call is validated by
the real SDK immediately, not after a whole plan is assembled. This
server is vendor-neutral -- any MCP client (Claude, GPT, Gemini,
LangGraph's MCP adapter, ...) calls these same tools the same way. Only
`multistack` (the real SDK) is imported here; no LLM-specific code lives
in this file.

Run standalone (stdio transport, for local MCP clients / testing):
    cd agentic/backend/src && python -m mcp_server

SCOPE: this agent's only job is to turn a request into a validated,
readable script and hand it back -- it never provisions anything itself.
Every tool is read-only and has no side effects; nothing anywhere in
mcp_tools/ calls a backend's create()/update()/delete(). Whether and how
the generated script actually runs is entirely up to whoever receives
it.

This module just defines the shared `mcp` instance and imports each
resource's tool module for its registration side effect -- one module
per resource under mcp_tools/, split out once a second resource
(Storage) landed, per the scaling point named in
docs/architecture-decision.md and agentic/backend/README.md. `mcp` has to exist
here *before* those imports run, since each mcp_tools/<resource>.py
does `from mcp_server import mcp` at its own top -- safe because Python
resolves that against this module's already-partially-executed
namespace, not a fresh one.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("multistack")

from mcp_tools import (  # noqa: E402,F401 -- see module docstring
    accelerator,
    billing,
    cnpg,
    controlplane,
    enricher,
    full_stack,
    gateway,
    inference,
    ingress_gateway,
    minio,
    observability,
    policy,
    portal,
    queue,
    rke2,
    route,
    storage,
    tokenizer,
    valkey,
)

if __name__ == "__main__":
    mcp.run()
