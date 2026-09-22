"""One module per resource's MCP tool -- mcp_server.py defines the shared
`mcp` instance and imports these for their registration side effect.
Split out once a second resource (Storage) landed, per the trigger named
in docs/architecture-decision.md and agentic/backend/README.md."""
