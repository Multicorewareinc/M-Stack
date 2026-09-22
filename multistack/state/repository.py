"""
CRUD over the `deployments` and `health_checks` tables.

StateManager works entirely in terms of these methods and the models in
`models.py`; nothing above this file writes SQL.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from .db import SQLiteStore
from .errors import DeploymentConflictError, DeploymentNotFoundError
from .models import Deployment, DeploymentStatus, HealthCheckResult, HealthStatus


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class DeploymentRepository:
    """Reads and writes deployment/health-check rows through `store`."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def create(self, name: str, component_type: str) -> Deployment:
        if self.get(name) is not None:
            raise DeploymentConflictError(
                f"a deployment named '{name}' already exists. StateManager.start() "
                "reuses an existing row instead of calling this directly."
            )
        now = _utcnow()
        self._store.execute(
            "INSERT INTO deployments "
            "(name, component_type, status, kubeconfig_path, error, created_at, updated_at) "
            "VALUES (?, ?, ?, NULL, NULL, ?, ?)",
            (name, component_type, DeploymentStatus.PENDING.value, now, now),
        )
        return self.get(name)

    def get(self, name: str) -> Optional[Deployment]:
        row = self._store.query_one("SELECT * FROM deployments WHERE name = ?", (name,))
        return self._row_to_deployment(row) if row else None

    def require(self, name: str) -> Deployment:
        deployment = self.get(name)
        if deployment is None:
            raise DeploymentNotFoundError(f"no deployment named '{name}' is recorded.")
        return deployment

    def list(self) -> List[Deployment]:
        rows = self._store.query_all("SELECT * FROM deployments ORDER BY name")
        return [self._row_to_deployment(r) for r in rows]

    def set_status(
        self, name: str, status: DeploymentStatus, *, error: Optional[str] = None
    ) -> Deployment:
        self.require(name)
        self._store.execute(
            "UPDATE deployments SET status = ?, error = ?, updated_at = ? WHERE name = ?",
            (status.value, error, _utcnow(), name),
        )
        return self.get(name)

    def set_component_type(self, name: str, component_type: str) -> Deployment:
        """Corrects the recorded kind of an existing deployment.

        Needed because a capability can be renamed. vLLM became the
        `inference` capability, so the code that used to write
        component_type "vllm" now writes "inference" -- while the row for
        an already-deployed vllm-qwen still said "vllm", because start()
        only set the type when creating the row. The result was one
        capability under two names, and `has_healthy("inference")` not
        seeing a deployment that was plainly there.
        """
        self.require(name)
        self._store.execute(
            "UPDATE deployments SET component_type = ?, updated_at = ? "
            "WHERE name = ?",
            (component_type, _utcnow(), name),
        )
        return self.get(name)

    def set_kubeconfig_path(self, name: str, kubeconfig_path: Optional[str]) -> Deployment:
        self.require(name)
        self._store.execute(
            "UPDATE deployments SET kubeconfig_path = ?, updated_at = ? WHERE name = ?",
            (kubeconfig_path, _utcnow(), name),
        )
        return self.get(name)

    def delete(self, name: str) -> None:
        self.require(name)
        self._store.execute("DELETE FROM deployments WHERE name = ?", (name,))

    def has_healthy(self, component_type: str) -> bool:
        """Whether any deployment of `component_type` is currently healthy.

        What dependency resolution checks before provisioning a component
        that requires another -- see StateManager.require_healthy_dependencies.
        Works for any component_type a caller passes; a component with no
        dependencies of its own (e.g. RKE2, at the bottom of the stack)
        simply never calls it.
        """
        row = self._store.query_one(
            "SELECT 1 FROM deployments WHERE component_type = ? AND status = ?",
            (component_type, DeploymentStatus.PROVISIONED.value),
        )
        return row is not None

    def record_health_check(
        self, deployment_name: str, status: HealthStatus, detail: str
    ) -> HealthCheckResult:
        self.require(deployment_name)
        checked_at = _utcnow()
        self._store.execute(
            "INSERT INTO health_checks (deployment_name, status, detail, checked_at) "
            "VALUES (?, ?, ?, ?)",
            (deployment_name, status.value, detail, checked_at),
        )
        return HealthCheckResult(
            deployment_name=deployment_name, status=status, detail=detail, checked_at=checked_at
        )

    def latest_health_check(self, deployment_name: str) -> Optional[HealthCheckResult]:
        row = self._store.query_one(
            "SELECT * FROM health_checks WHERE deployment_name = ? "
            "ORDER BY id DESC LIMIT 1",
            (deployment_name,),
        )
        if row is None:
            return None
        return HealthCheckResult(
            deployment_name=row["deployment_name"],
            status=HealthStatus(row["status"]),
            detail=row["detail"],
            checked_at=row["checked_at"],
        )

    @staticmethod
    def _row_to_deployment(row: object) -> Deployment:
        return Deployment(
            name=row["name"],
            component_type=row["component_type"],
            status=DeploymentStatus(row["status"]),
            kubeconfig_path=row["kubeconfig_path"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
