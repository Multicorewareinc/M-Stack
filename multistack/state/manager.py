"""
Records the lifecycle and health of deployments in SQLite. It does not
deploy anything.

Each component's own backend (e.g. RKE2Backend in backends/rke2_client.py)
is what installs/joins/tears down its infrastructure, and may keep its own
state for that purpose (RKE2Backend's JSON ClusterState -- join token, node
list). This module adds the cross-component view that file can't provide:
whoever drives a backend call -- a script, an orchestrator, any component's
backend -- reports into StateManager as it goes, tagging its own
`component_type` string, so "what's deployed and how healthy is it" is one
SQLite query across every component instead of a directory of per-component
JSON files.

Typical call shape around a backend's create():

    state = StateManager()
    state.start("ai-cluster", component_type="rke2")
    try:
        backend.create(cluster)
    except Exception as exc:
        state.mark_failed("ai-cluster", str(exc))
        raise
    state.mark_healthy("ai-cluster", kubeconfig_path=cluster.kubeconfig_path)
"""
from __future__ import annotations

from typing import Iterable, List, Optional

from .config import StateConfig
from .db import SQLiteStore
from .errors import DependencyResolutionError
from .models import Deployment, DeploymentStatus, HealthCheckResult, HealthStatus
from .repository import DeploymentRepository

# A recorded health probe maps onto a deployment status -- except UNKNOWN,
# which means "couldn't tell" and deliberately leaves status alone rather
# than regressing a HEALTHY deployment over one inconclusive probe.
_HEALTH_TO_DEPLOYMENT_STATUS = {
    HealthStatus.HEALTHY: DeploymentStatus.PROVISIONED,
    HealthStatus.DEGRADED: DeploymentStatus.DEGRADED,
    HealthStatus.UNHEALTHY: DeploymentStatus.FAILED,
}


class StateManager:
    """The platform state layer's single entry point: pure recording and
    reading, never an operation on real infrastructure. See the module
    docstring for the call shape other services use around it."""

    def __init__(self, config: Optional[StateConfig] = None) -> None:
        self._config = config or StateConfig()
        self._store = SQLiteStore(self._config)
        self._repo = DeploymentRepository(self._store)

    # -- lifecycle recording ---------------------------------------------
    def start(self, name: str, component_type: str) -> Deployment:
        """Records that a deployment has begun. `component_type` is any
        string a caller chooses to identify its kind of component (e.g.
        "rke2", "storage") -- this layer doesn't privilege any one of them.
        Call this right before the real create()/update() call starts.
        Safe to call again on an existing deployment (a reconcile) -- it
        just marks PROVISIONING."""
        existing = self._repo.get(name)
        if existing is None:
            self._repo.create(name, component_type)
        elif existing.component_type != component_type:
            # The caller is the authority on what kind of thing this is,
            # and capabilities do get renamed -- vLLM became `inference`,
            # so rows written as "vllm" have to follow rather than leave
            # one capability recorded under two names. Adopting the new
            # type is the only option that keeps
            # require_healthy_dependencies() able to find it.
            self._repo.set_component_type(name, component_type)
        return self._repo.set_status(name, DeploymentStatus.PROVISIONING)

    def set_status(self, name: str, status: DeploymentStatus) -> Deployment:
        """Records an intermediate progress state (e.g. VALIDATING,
        DELETING) while the real operation is still running."""
        return self._repo.set_status(name, status)

    def mark_healthy(
        self,
        name: str,
        *,
        kubeconfig_path: Optional[str] = None,
        detail: str = "deployment succeeded",
    ) -> Deployment:
        """Records that the deployment finished and is healthy."""
        if kubeconfig_path is not None:
            self._repo.set_kubeconfig_path(name, kubeconfig_path)
        self._repo.record_health_check(name, HealthStatus.HEALTHY, detail)
        return self._repo.set_status(name, DeploymentStatus.PROVISIONED)

    def mark_degraded(self, name: str, detail: str) -> Deployment:
        """Records that the deployment is up but not fully healthy."""
        self._repo.record_health_check(name, HealthStatus.DEGRADED, detail)
        return self._repo.set_status(name, DeploymentStatus.DEGRADED)

    def mark_failed(self, name: str, error: str) -> Deployment:
        """Records that the deployment (or an operation on it) failed."""
        self._repo.record_health_check(name, HealthStatus.UNHEALTHY, error)
        return self._repo.set_status(name, DeploymentStatus.FAILED, error=error)

    def remove(self, name: str) -> None:
        """Drops the record for `name`. Call this after the real teardown
        (the component's own backend.delete() or equivalent) has already
        succeeded -- this never tears anything down itself."""
        self._repo.delete(name)

    # -- health checks ----------------------------------------------------
    def record_health_check(
        self, name: str, status: HealthStatus, detail: str
    ) -> HealthCheckResult:
        """Logs one health probe a caller already ran against the real
        infrastructure (this module makes no such calls itself), updating
        `status` when the result maps onto a lifecycle state. UNKNOWN
        leaves the current status alone rather than regressing it."""
        result = self._repo.record_health_check(name, status, detail)
        mapped = _HEALTH_TO_DEPLOYMENT_STATUS.get(status)
        if mapped is not None:
            self._repo.set_status(name, mapped)
        return result

    # -- dependency queries ------------------------------------------------
    def require_healthy_dependencies(self, requires: Iterable[str]) -> None:
        """Raises DependencyResolutionError for the first capability in
        `requires` with no healthy deployment recorded. A query against
        already-recorded state, not a check on live infrastructure --
        callers use it before starting their own deployment."""
        for capability in requires:
            if not self._repo.has_healthy(capability):
                raise DependencyResolutionError(
                    f"no healthy deployment provides the '{capability}' capability."
                )

    # -- reading ------------------------------------------------------
    def get(self, name: str) -> Optional[Deployment]:
        return self._repo.get(name)

    def list_deployments(self) -> List[Deployment]:
        return self._repo.list()

    def latest_health_check(self, name: str) -> Optional[HealthCheckResult]:
        return self._repo.latest_health_check(name)

    def close(self) -> None:
        self._store.close()
