"""
Block storage capability.

    from multistack.storage import Storage, StorageBackend

    StorageBackend().create(
        Storage(type="longhorn", kubeconfig_path=kc), nodes=nodes
    )

`StorageBackend` is the only backend a caller touches. It reads `type` off
the spec and delegates to that implementation's driver, so a second
implementation means adding a driver module and one registry entry — no
caller changes, and no `if type == ...` anywhere outside this package.

    spec.py       the Storage spec and each implementation's options
    base.py       the driver contract, and the errors a caller can catch
    registry.py   type -> driver, and the backend that dispatches
    drivers/      one module per implementation, loaded on demand

Drivers are not re-exported here. A caller that names `LonghornDriver` has
given up the interchangeability this package exists to provide, and
importing one eagerly would defeat the lazy loading in `registry.py`.
Failures are catchable without naming an implementation: every driver's
errors subclass `StorageError` or `StoragePrerequisiteError`.
"""
from .base import StorageDriver, StorageError, StoragePrerequisiteError
from .registry import DRIVERS, StorageBackend
from .spec import ACCESS_MODES, SUPPORTED_TYPES, LonghornOptions, Storage, VolumeClaim

__all__ = [
    "Storage",
    "StorageBackend",
    "StorageDriver",
    "StorageError",
    "StoragePrerequisiteError",
    "LonghornOptions",
    "VolumeClaim",
    "ACCESS_MODES",
    "SUPPORTED_TYPES",
    "DRIVERS",
]
