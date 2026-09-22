"""
The state layer's value objects.

One row in `deployments` per component instance, mirrored here as a frozen
model so callers get the same validated-on-construction guarantee every
other spec in this SDK does. `component_type` is a free string rather than
an enum on purpose -- `StateManager` never validates it against a fixed
list, so any component can register under its own name with no migration.
"""
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict


class DeploymentStatus(str, Enum):
    PENDING = "pending"
    VALIDATING = "validating"
    PROVISIONING = "provisioning"
    PROVISIONED = "provisioned"
    DEGRADED = "degraded"
    FAILED = "failed"
    DELETING = "deleting"


class HealthStatus(str, Enum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class Deployment(BaseModel):
    """A deployment as it currently stands in the state store."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    component_type: str
    status: DeploymentStatus
    kubeconfig_path: Optional[str] = None
    error: Optional[str] = None
    created_at: str
    updated_at: str


class HealthCheckResult(BaseModel):
    """One health probe against a deployment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    deployment_name: str
    status: HealthStatus
    detail: str
    checked_at: str
