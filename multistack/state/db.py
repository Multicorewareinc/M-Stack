"""
Adapter around sqlite3.

DeploymentRepository interacts with this class instead of touching sqlite3
directly -- the same split HelmManager keeps from pyhelm3 via
PyHelm3Client: one place that knows the schema and connection settings,
everything above it works with rows and models.

SQLite is run "as a local service" in the sense that this file *is* the
service: no separate daemon, just a database file at `db_path` that every
process on the machine opens through this adapter. WAL mode is what makes
that safe -- readers (a health-check loop, a status CLI) don't block the
writer, which is the one thing that matters once more than one script
touches the same file at once.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .config import StateConfig

SCHEMA = """
CREATE TABLE IF NOT EXISTS deployments (
    name            TEXT PRIMARY KEY,
    component_type  TEXT NOT NULL,
    status          TEXT NOT NULL,
    kubeconfig_path TEXT,
    error           TEXT,
    vault_path      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS health_checks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    deployment_name TEXT NOT NULL REFERENCES deployments(name) ON DELETE CASCADE,
    status          TEXT NOT NULL,
    detail          TEXT NOT NULL,
    checked_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_health_checks_deployment
    ON health_checks(deployment_name, checked_at);
"""


class SQLiteStore:
    """Owns the connection to `config.db_path` and its schema."""

    def __init__(self, config: StateConfig | None = None) -> None:
        self._config = config or StateConfig()
        Path(self._config.db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(Path(self._config.db_path).expanduser()),
            timeout=self._config.busy_timeout_ms / 1000,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Adds columns to a `deployments` table that predates them.

        `CREATE TABLE IF NOT EXISTS` above only shapes a brand-new file --
        a `state.db` written before `vault_path` existed already has a
        `deployments` table, so that statement is a no-op against it and
        the column never appears on its own. Every later read/write of
        `vault_path` (StateManager.mark_secret_stored, _row_to_deployment)
        would then fail with "no such column" against exactly the state
        files this layer is supposed to keep working across a version
        bump. Checked and run before anything else touches the table.
        """
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(deployments)")}
        if "vault_path" not in columns:
            self._conn.execute("ALTER TABLE deployments ADD COLUMN vault_path TEXT")

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._conn:
            return self._conn.execute(sql, params)

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self._conn.execute(sql, params).fetchone()

    def query_all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self._conn.execute(sql, params).fetchall()

    def close(self) -> None:
        self._conn.close()
