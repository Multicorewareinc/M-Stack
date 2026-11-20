"""
The viewer's read model and HTTP server (standard library only).

`StateReader` knows the state schema (see `multistack/state/db.py`) and
nothing else: it opens the file read-only, per call, and returns plain dicts.
`create_server` wraps it in a `ThreadingHTTPServer` that serves a JSON API
under `/api/` and, when a built UI directory is given, the single-page app.

Endpoints (all GET, all JSON, `Cache-Control: no-store`):

    /api/info                    where the db is, whether it exists, row counts
    /api/summary                 counts by status and by component type
    /api/deployments             every deployment + its latest health check
                                 (?status= ?component_type= ?search=)
    /api/deployment?name=        one deployment + its full health-check history
                                 (`name` is a query parameter, not a path
                                 segment: route names look like "ns/name")
    /api/health-checks           newest-first health checks across everything
                                 (?limit= ?deployment= ?status=)

A missing database is not an error -- it is the normal state of a machine
that has not deployed anything yet -- so it answers 200 with empty lists and
`exists: false`. A database that exists but cannot be read answers 503 with
the reason.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..config import DEFAULT_DB_PATH

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_HEALTH_LIMIT = 1000
DEFAULT_HEALTH_LIMIT = 100

MAX_HISTORY = 500
LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})

# Where a built UI can be found, best first: shipped inside the package (what `pip install` gives
# you -- see ui/state-viewer's `build:sdk` script), else the dev build in a source checkout.
_PACKAGED_UI = Path(__file__).resolve().parent / "web"
_REPO_UI_DIST = Path(__file__).resolve().parents[3] / "ui" / "state-viewer" / "dist"


def default_ui_dir() -> Path | None:
    for candidate in (_PACKAGED_UI, _REPO_UI_DIST):
        if (candidate / "index.html").is_file():
            return candidate
    return None


_DEPLOYMENT_COLUMNS = "name, component_type, status, kubeconfig_path, error, created_at, updated_at"


class StateUnreadable(Exception):
    """The database file exists but could not be opened or queried."""


def resolve_db_path(explicit: str | None = None) -> Path:
    """`--db`, else $MULTISTACK_STATE_DB, else the SDK default -- the same
    precedence `default_state_manager()` uses, so the viewer looks where the
    backends write."""
    raw = explicit or os.environ.get("MULTISTACK_STATE_DB") or DEFAULT_DB_PATH
    return Path(raw).expanduser()


def _now() -> str:
    return datetime.now(UTC).isoformat()


class StateReader:
    """Read-only access to a state database. Opens a fresh connection per
    call, so it holds no lock and always sees the writer's latest commit."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    def exists(self) -> bool:
        return self.db_path.is_file()

    @staticmethod
    def _open(path: Path) -> sqlite3.Connection:
        # mode=ro: SQLite refuses every write, and will not create the file. `as_uri()` percent-encodes
        # the path; hand-building "file:" + path breaks on '#', '?' and '%' (the path is silently cut
        # short, so the viewer can open a different file, or none).
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def _connect(self) -> sqlite3.Connection:
        return self._open(self.db_path)

    def _run(
        self, conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]
    ) -> list[dict[str, Any]]:
        try:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        if not self.exists():
            return []
        if self.db_path.stat().st_size == 0:
            return []  # a zero-byte file is an empty store (the SDK creates the schema on first write)
        try:
            try:
                return self._run(self._connect(), sql, params)
            except sqlite3.OperationalError as exc:
                if "unable to open" not in str(exc).lower():
                    raise
                # A WAL-mode database needs a -shm file next to it, which a reader cannot create in a
                # folder it may not write to (read-only mount, another user's directory, a copied-in
                # snapshot). Query a private copy instead -- a point-in-time view, never the original.
                return self._query_snapshot(sql, params)
        except sqlite3.Error as exc:
            raise StateUnreadable(self._explain(exc)) from exc

    def _query_snapshot(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        with tempfile.TemporaryDirectory(prefix="multistack-state-") as tmp:
            target = Path(tmp) / "state.db"
            try:
                for suffix in ("", "-wal", "-shm"):
                    source = Path(str(self.db_path) + suffix)
                    if source.is_file():
                        shutil.copy2(source, Path(str(target) + suffix))
            except OSError as exc:
                raise StateUnreadable(
                    f"{self.db_path} cannot be read: {exc.strerror or exc}"
                ) from exc
            return self._run(self._open(target), sql, params)

    def _explain(self, exc: sqlite3.Error) -> str:
        text = str(exc)
        if "no such table" in text:
            return (
                f"{self.db_path} is a SQLite database but not a multistack state database "
                f"({text}). Check --db / MULTISTACK_STATE_DB."
            )
        if "not a database" in text:
            return f"{self.db_path} is not a SQLite database."
        return text

    # -- reads -------------------------------------------------------------
    def info(self) -> dict[str, Any]:
        base: dict[str, Any] = {
            "db_path": str(self.db_path),
            "exists": self.exists(),
            "deployments": 0,
            "health_checks": 0,
            "generated_at": _now(),
        }
        if not base["exists"]:
            return base
        base["deployments"] = self._count("deployments")
        base["health_checks"] = self._count("health_checks")
        return base

    def _count(self, table: str) -> int:
        rows = self._query(
            f"SELECT COUNT(*) AS n FROM {table}"
        )  # table is a constant from this module
        return rows[0]["n"] if rows else 0  # no rows back = an empty (zero-byte) file

    def summary(self) -> dict[str, Any]:
        by_status = self._query(
            "SELECT status, COUNT(*) AS count FROM deployments GROUP BY status ORDER BY status"
        )
        by_type = self._query(
            "SELECT component_type, COUNT(*) AS count FROM deployments GROUP BY component_type ORDER BY component_type"
        )
        latest = self._query("SELECT MAX(updated_at) AS latest FROM deployments")
        return {
            "total": sum(r["count"] for r in by_status),
            "by_status": by_status,
            "by_component_type": by_type,
            "last_updated": latest[0]["latest"] if latest else None,
            "generated_at": _now(),
        }

    def deployments(
        self,
        status: str | None = None,
        component_type: str | None = None,
        search: str | None = None,
    ) -> list[dict[str, Any]]:
        conds, params = [], []
        if status:
            conds.append("d.status = ?")
            params.append(status)
        if component_type:
            conds.append("d.component_type = ?")
            params.append(component_type)
        if search:
            conds.append("(d.name LIKE ? ESCAPE '\\' OR d.error LIKE ? ESCAPE '\\')")
            like = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            params += [like, like]
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        # Latest health check per deployment: the row with the greatest id for that name.
        rows = self._query(
            f"""
            SELECT d.name, d.component_type, d.status, d.kubeconfig_path, d.error,
                   d.created_at, d.updated_at,
                   h.status AS health_status, h.detail AS health_detail, h.checked_at AS health_checked_at
            FROM deployments d
            LEFT JOIN health_checks h
              ON h.id = (SELECT MAX(id) FROM health_checks WHERE deployment_name = d.name)
            {where}
            ORDER BY d.component_type, d.name
            """,
            tuple(params),
        )
        return [_shape_deployment(r) for r in rows]

    def deployment(self, name: str) -> dict[str, Any] | None:
        rows = self._query(f"SELECT {_DEPLOYMENT_COLUMNS} FROM deployments WHERE name = ?", (name,))
        if not rows:
            return None
        history = self._query(
            "SELECT id, status, detail, checked_at FROM health_checks WHERE deployment_name = ? "
            "ORDER BY id DESC LIMIT ?",
            (name, MAX_HISTORY),
        )
        return {**rows[0], "health_checks": history}

    def health_checks(
        self,
        limit: int = DEFAULT_HEALTH_LIMIT,
        deployment: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        conds, params = [], []
        if deployment:
            conds.append("h.deployment_name = ?")
            params.append(deployment)
        if status:
            conds.append("h.status = ?")
            params.append(status)
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        limit = max(1, min(limit, MAX_HEALTH_LIMIT))
        return self._query(
            f"""
            SELECT h.id, h.deployment_name, d.component_type, h.status, h.detail, h.checked_at
            FROM health_checks h
            LEFT JOIN deployments d ON d.name = h.deployment_name
            {where}
            ORDER BY h.id DESC
            LIMIT ?
            """,
            (*tuple(params), limit),
        )


def _shape_deployment(row: dict[str, Any]) -> dict[str, Any]:
    health = None
    if row["health_status"] is not None:
        health = {
            "status": row["health_status"],
            "detail": row["health_detail"],
            "checked_at": row["health_checked_at"],
        }
    out = {k: row[k] for k in row if not k.startswith("health_")}
    out["latest_health_check"] = health
    return out


# -- HTTP ----------------------------------------------------------------------


def _first(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values and values[0] != "" else None


def allowed_hosts(bind_host: str) -> frozenset[str] | None:
    """Host header values accepted for a server bound to `bind_host`, or None for "any".

    The viewer has no authentication, so a web page on another site must not be able to reach it by
    pointing a DNS name at 127.0.0.1 (DNS rebinding) -- such a request carries the attacker's name in
    `Host`. Loopback binds accept only loopback names; binding to a specific address also accepts that
    address; binding to all interfaces is an explicit opt-out (and already warns at startup)."""
    if bind_host in ("0.0.0.0", "::", ""):
        return None
    return LOOPBACK_NAMES | {bind_host.lower()}


def _host_name(header: str) -> str:
    header = header.strip().lower()
    if header.startswith("["):  # [::1]:8765
        return header[1 : header.find("]")] if "]" in header else header
    return header.rsplit(":", 1)[0] if header.count(":") == 1 else header


class _Handler(BaseHTTPRequestHandler):
    reader: StateReader
    static_dir: Path | None
    hosts: frozenset[str] | None = None
    server_version = "multistack-state-viewer"

    # -- plumbing
    def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._send_json({"error": {"message": message}}, status)

    def log_message(self, format: str, *args: Any) -> None:
        if os.environ.get("MULTISTACK_VIEWER_ACCESS_LOG"):
            super().log_message(format, *args)

    # -- routing
    def do_GET(self) -> None:
        if self.hosts is not None and _host_name(self.headers.get("Host", "")) not in self.hosts:
            return self._error(HTTPStatus.FORBIDDEN, "Unrecognised Host header.")
        url = urlparse(self.path)
        if url.path.startswith("/api/"):
            return self._api(url.path, parse_qs(url.query))
        return self._static(url.path)

    def do_POST(self) -> None:
        self._error(HTTPStatus.METHOD_NOT_ALLOWED, "The state viewer is read-only.")

    do_PUT = do_PATCH = do_DELETE = do_POST

    def _api(self, path: str, q: dict[str, list[str]]) -> None:
        try:
            if path == "/api/info":
                return self._send_json(self.reader.info())
            if path == "/api/summary":
                return self._send_json(self.reader.summary())
            if path == "/api/deployments":
                return self._send_json(
                    self.reader.deployments(
                        _first(q, "status"), _first(q, "component_type"), _first(q, "search")
                    )
                )
            if path == "/api/deployment":
                name = _first(q, "name")
                if not name:
                    return self._error(
                        HTTPStatus.UNPROCESSABLE_ENTITY, "Missing 'name' query parameter."
                    )
                found = self.reader.deployment(name)
                if found is None:
                    return self._error(HTTPStatus.NOT_FOUND, f"Deployment '{name}' not found.")
                return self._send_json(found)
            if path == "/api/health-checks":
                try:
                    limit = int(_first(q, "limit") or DEFAULT_HEALTH_LIMIT)
                except ValueError:
                    return self._error(
                        HTTPStatus.UNPROCESSABLE_ENTITY, "'limit' must be an integer."
                    )
                return self._send_json(
                    self.reader.health_checks(limit, _first(q, "deployment"), _first(q, "status"))
                )
            return self._error(HTTPStatus.NOT_FOUND, f"No such endpoint: {path}")
        except StateUnreadable as exc:
            return self._error(
                HTTPStatus.SERVICE_UNAVAILABLE, f"State database could not be read: {exc}"
            )

    def _static(self, path: str) -> None:
        root = self.static_dir
        if root is None or not (root / "index.html").is_file():
            return self._error(
                HTTPStatus.NOT_FOUND,
                "No UI build found. Build ui/state-viewer (npm run build) or pass --static; the API is under /api/.",
            )
        target = (root / path.lstrip("/")).resolve()
        # Stay inside the build directory (no ../ escapes); anything that is not a real file is
        # a client-side route, so it gets the SPA shell.
        if root.resolve() not in target.parents or not target.is_file():
            target = root / "index.html"
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header(
            "Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # http.server turns SO_REUSEADDR on so a restarted server can rebind at once. On Windows that same
    # option lets a *second* process bind a port that is already being listened on, so "port in use"
    # is never reported and two viewers silently share one port. Only enable it where it is safe.
    allow_reuse_address = os.name != "nt"


def create_server(
    db_path: Path,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    static_dir: Path | None = None,
) -> ThreadingHTTPServer:
    """Build (but do not start) the server. `port=0` picks a free port."""
    handler = type(
        "StateViewerHandler",
        (_Handler,),
        {
            "reader": StateReader(db_path),
            "static_dir": Path(static_dir) if static_dir else None,
            "hosts": allowed_hosts(host),
        },
    )
    return _Server((host, port), handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m multistack.state.viewer",
        description="Serve a read-only view of the SDK's state database.",
    )
    parser.add_argument(
        "--db", help="state database (default: $MULTISTACK_STATE_DB, else ~/.multistack/state.db)"
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"bind address (default {DEFAULT_HOST}; the viewer has no auth)",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--static", help="built UI directory (default: the UI shipped with the SDK)"
    )
    args = parser.parse_args(argv)

    db_path = resolve_db_path(args.db)
    static = Path(args.static) if args.static else default_ui_dir()
    try:
        server = create_server(db_path, args.host, args.port, static)
    except OSError as exc:
        print(
            f"Cannot listen on {args.host}:{args.port} ({exc.strerror or exc}). "
            "Is another viewer already running? Pick a free port with --port.",
            file=sys.stderr,
        )
        return 2
    shown = "(not created yet)" if not db_path.is_file() else ""
    print(f"State database: {db_path} {shown}".rstrip())
    print(f"Serving on http://{args.host}:{server.server_address[1]}  (read-only; Ctrl+C to stop)")
    if static is None:
        print("No UI build found -- API only, under /api/. Build ui/state-viewer to get the page.")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            "WARNING: bound to a non-loopback address and the viewer has no authentication.",
            file=sys.stderr,
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
