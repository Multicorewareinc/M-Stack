"""
Cluster observability capability: metrics, dashboards and alerting.

    from multistack.observability import Observability, ObservabilityBackend

    endpoint = ObservabilityBackend().create(
        Observability(kubeconfig_path=kc, storage_class="longhorn")
    )

`ObservabilityBackend` is the only backend a caller touches. It reads
`type` off the spec and delegates to that implementation's driver, so a
second implementation means adding a driver module and one registry entry
— no caller changes, and no `if type == ...` anywhere outside this
package.

    spec.py       the Observability spec and each implementation's options
    base.py       the driver contract, and the errors a caller can catch
    registry.py   type -> driver, and the backend that dispatches
    drivers/      one module per implementation, loaded on demand

Drivers are not re-exported here. A caller that names
`KubePrometheusStackDriver` has given up the interchangeability this
package exists to provide. Failures are catchable without naming an
implementation: every driver's errors subclass `ObservabilityError` or
`ObservabilityPrerequisiteError`.
"""
from .base import (
    ObservabilityDriver,
    ObservabilityError,
    ObservabilityPrerequisiteError,
)
from .registry import DRIVERS, ObservabilityBackend
from .spec import (
    SUPPORTED_TYPES,
    KubePrometheusStackOptions,
    Observability,
)

__all__ = [
    "Observability",
    "ObservabilityBackend",
    "ObservabilityDriver",
    "ObservabilityError",
    "ObservabilityPrerequisiteError",
    "KubePrometheusStackOptions",
    "SUPPORTED_TYPES",
    "DRIVERS",
]
