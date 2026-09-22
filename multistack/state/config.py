"""
Configuration for the state layer.

`db_path` defaults to `~/.multistack/state.db` -- a path a normal user can
always write to, with no setup, which is what every backend's
`@track_create`/`@track_delete` relies on via `default_state_manager()`.
`~` is expanded by `SQLiteStore`, not here, so this stays a plain string.

Running this as a rooted system service instead (one `state.db` shared by
every user on the machine) is still one line away: pass
`StateConfig(db_path="/var/lib/platform/state.db")` explicitly wherever
you construct a `StateManager` for that deployment. It is opt-in rather
than the default because nothing in this SDK runs that way today, and a
default only reachable by root was a footgun for everyone else: the
natural-looking `StateManager()` silently pointed at a directory an
ordinary user cannot create, while the thing every real caller actually
uses (`default_state_manager()`) pointed somewhere else entirely.
"""
from pydantic import BaseModel, ConfigDict, model_validator

from .errors import StateConfigurationError

DEFAULT_DB_PATH = "~/.multistack/state.db"


class StateConfig(BaseModel):
    """Configuration used by SQLiteStore."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    db_path: str = DEFAULT_DB_PATH
    # How long SQLite waits on a locked database before giving up to a
    # caller instead. Matters once more than one process touches the same
    # file, which is the point of running it "as a local service".
    busy_timeout_ms: int = 5000

    @model_validator(mode="after")
    def _check(self) -> "StateConfig":
        if not self.db_path:
            raise StateConfigurationError("db_path cannot be empty.")
        if self.busy_timeout_ms < 0:
            raise StateConfigurationError("busy_timeout_ms cannot be negative.")
        return self
