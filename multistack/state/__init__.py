"""
Recording the health and lifecycle of infrastructure this SDK deploys.

This is a pure state recorder -- it never provisions anything itself.
Whatever performs the real work (a component's own backend
create()/update()/delete(), e.g. RKE2Backend today, any other component's
backend tomorrow) reports into it as it goes, tagging each deployment with
whatever `component_type` string identifies its kind of component:

    from multistack.state import StateManager

    state = StateManager()
    state.start("ai-cluster", component_type="rke2")
    try:
        kubeconfig = backend.create(cluster)          # the real work
    except Exception as exc:
        state.mark_failed("ai-cluster", str(exc))
        raise
    state.mark_healthy("ai-cluster", kubeconfig_path=kubeconfig)

`StateManager` is the whole surface, backed by a single SQLite file at
`~/.multistack/state.db` by default (see `StateConfig` to point it
elsewhere -- tests use a tmp path, for isolation between runs rather than
out of necessity).

Why SQLite here and JSON files in backends/rke2_client.py
-----------------------------------------------------------
RKE2Backend already keeps its own state (join token, node list) as JSON
under `~/.multistack/state/` -- it needs that to be idempotent and to diff
node sets, and it predates this layer, and is left exactly as it is. This
module answers a different question: not "what did the backend
provision", but "what is deployed, what shape is it in" -- across every
component this SDK will eventually manage, not just RKE2's own internals.
A single queryable file is what makes "list everything that's DEGRADED
right now" a SELECT instead of a directory walk over N JSON files.

`component_type` is a free-form string this layer never validates against
a fixed list -- "rke2" is just the first caller to use it; any other
component calls the same methods with its own name.
"""
from .config import DEFAULT_DB_PATH, StateConfig
from .db import SQLiteStore
from .errors import (
    DependencyResolutionError,
    DeploymentConflictError,
    DeploymentNotFoundError,
    StateConfigurationError,
    StateError,
)
from .manager import StateManager
from .models import Deployment, DeploymentStatus, HealthCheckResult, HealthStatus
from .tracking import default_state_manager, track_create, track_delete, track_update

__all__ = [
    "StateManager",
    "StateConfig",
    "DEFAULT_DB_PATH",
    "default_state_manager",
    "track_create",
    "track_delete",
    "track_update",
    "Deployment",
    "DeploymentStatus",
    "HealthCheckResult",
    "HealthStatus",
    "SQLiteStore",
    "StateError",
    "StateConfigurationError",
    "DependencyResolutionError",
    "DeploymentNotFoundError",
    "DeploymentConflictError",
]
