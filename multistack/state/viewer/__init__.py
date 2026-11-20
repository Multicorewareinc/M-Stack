"""
Read-only viewer for the state database.

`multistack.state` records what the SDK deployed into a SQLite file; this
package serves that file over a small local HTTP API (and, optionally, the
built `ui/state-viewer` page) so the person running the SDK can see what is
deployed and what shape it is in without re-running a script.

It only ever *reads*: the database is opened with SQLite's `mode=ro`, so the
viewer can never create, migrate, or modify a state file, and it is safe to
run while an SDK script is writing (the database is in WAL mode).

Standard library only -- the SDK's one hard dependency stays pydantic.

    python -m multistack.state.viewer                  # ~/.multistack/state.db
    python -m multistack.state.viewer --db ./state.db --port 8765
"""

from .server import StateReader, create_server, main

__all__ = ["StateReader", "create_server", "main"]
