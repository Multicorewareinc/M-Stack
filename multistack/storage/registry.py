"""
Which implementation serves which `type`, and the backend that dispatches.

Entries are `(module_path, class_name)` rather than imported classes, so a
driver's module — and everything it imports — loads only when that `type`
is actually chosen. Longhorn shells out to helm and kubectl and needs
nothing but the standard library, but a future driver reaching for a client
library must not make `import multistack` depend on it, and one broken
optional dependency must not break importing the SDK at all.

`CapabilityBackend.driver_for()` does the resolving; see
`multistack/capability.py`.
"""
from __future__ import annotations

from typing import List, Optional

from .. import kube
from ..capability import CapabilityBackend
from ..state.tracking import track_create, track_delete
from .base import StorageError
from .spec import Storage, VolumeClaim

# type -> (module, class). Keep in step with spec.SUPPORTED_TYPES and
# spec.OPTIONS_FOR_TYPE; tests/test_capability.py enforces it, and also
# resolves every entry so a typo here fails a test rather than a deploy.
DRIVERS = {
    "longhorn": ("multistack.storage.drivers.longhorn", "LonghornDriver"),
}


class StorageBackend(CapabilityBackend):
    """Provisions block storage, dispatching on the spec's `type`.

    Everything generic — validation, dependency checks, driver resolution,
    caching — comes from `CapabilityBackend`. All that remains is the
    capability's own method surface, one line each.
    """

    DRIVERS = DRIVERS

    def check_prerequisites(self, storage: Storage, nodes: Optional[List] = None) -> List[str]:
        return self.driver_for(storage).check_prerequisites(storage, nodes)

    def install_prerequisites(self, storage: Storage, nodes: List) -> None:
        """Takes the spec as well as the nodes, unlike the driver: the spec
        is what says whose packages to install."""
        return self.driver_for(storage).install_prerequisites(nodes)

    @track_create("storage", name_of=lambda storage: storage.type)
    def create(self, storage: Storage, nodes: Optional[List] = None) -> str:
        return self.driver_for(storage).create(storage, nodes)

    @track_delete(name_of=lambda storage: storage.type)
    def delete(self, storage: Storage) -> None:
        return self.driver_for(storage).delete(storage)

    # -- claims ----------------------------------------------------------
    # Not driver methods. A PersistentVolumeClaim is a Kubernetes object
    # that names a StorageClass, and every block-storage implementation
    # produces one — so there is nothing here for an implementation to do
    # differently, and implementing it per driver would be the same code
    # twice. `storage` is taken as an argument only to resolve which
    # cluster and which StorageClass.
    def _claim_class(self, storage: Storage, claim: VolumeClaim) -> str:
        return claim.storage_class or storage.storage_class_name

    def create_claim(
        self,
        storage: Storage,
        claim: VolumeClaim,
        *,
        wait: bool = True,
        timeout: int = 300,
    ) -> str:
        """Creates the claim and, by default, waits for it to bind.

        Idempotent: applying an existing claim is a no-op rather than a
        conflict, so a provisioning script can be re-run.

        Waiting matters more than it looks. An unbound PVC is not an error
        — it stays Pending indefinitely, and whatever mounts it then fails
        instead, which is where the blame lands. Returns the StorageClass
        it bound against.
        """
        storage.validate()
        storage_class = self._claim_class(storage, claim)
        kube.apply(
            storage.kubeconfig_path,
            claim.manifest(storage_class),
            error_cls=StorageError,
        )
        print(f"[storage] claim {claim.namespace}/{claim.name} "
              f"({claim.size}, {claim.access_mode}) on '{storage_class}'")
        if wait:
            kube.wait_for_phase(
                storage.kubeconfig_path, "pvc", claim.name, claim.namespace,
                "Bound", timeout=timeout, error_cls=StorageError,
            )
            print(f"[storage] claim {claim.namespace}/{claim.name} is Bound")
        return storage_class

    def resize_claim(
        self,
        storage: Storage,
        claim: VolumeClaim,
        new_size: str,
        *,
        timeout: int = 300,
    ) -> None:
        """Grows a claim.

        Kubernetes can only grow a PVC, never shrink one, and the
        StorageClass needs `allowVolumeExpansion` (Longhorn's has it). Both
        are checked here rather than left to a patch that half-applies:
        a shrink is rejected by the API server with a message about
        immutability that doesn't mention shrinking, and a class without
        expansion accepts the patch and silently never resizes.
        """
        storage.validate()
        storage_class = self._claim_class(storage, claim)

        expansion = kube.kubectl(
            storage.kubeconfig_path, "get", "sc", storage_class,
            "-o", "jsonpath={.allowVolumeExpansion}",
            check=False, error_cls=StorageError,
        ).strip()
        if expansion != "true":
            raise StorageError(
                f"StorageClass '{storage_class}' does not allow volume "
                f"expansion (allowVolumeExpansion={expansion or 'unset'}), so "
                "resizing this claim would be accepted and then never happen."
            )

        current = kube.kubectl(
            storage.kubeconfig_path, "get", "pvc", claim.name,
            "-n", claim.namespace,
            "-o", "jsonpath={.spec.resources.requests.storage}",
            error_cls=StorageError,
        ).strip()
        if _as_bytes(new_size) < _as_bytes(current):
            raise StorageError(
                f"cannot shrink {claim.namespace}/{claim.name} from {current} "
                f"to {new_size} — Kubernetes only supports growing a PVC."
            )

        kube.kubectl(
            storage.kubeconfig_path, "patch", "pvc", claim.name,
            "-n", claim.namespace, "--type=merge",
            "-p", f'{{"spec":{{"resources":{{"requests":{{"storage":"{new_size}"}}}}}}}}',
            error_cls=StorageError,
        )
        claim.size = new_size
        print(f"[storage] claim {claim.namespace}/{claim.name} resized "
              f"{current} -> {new_size}")

    def delete_claim(
        self,
        storage: Storage,
        claim: VolumeClaim,
        *,
        wait: bool = True,
        timeout: int = 300,
    ) -> None:
        """Deletes the claim, and by default waits until it is gone.

        DESTROYS THE DATA. A claim's volume goes with it under the default
        Delete reclaim policy, which is what Longhorn's StorageClass uses.

        Waiting is not cosmetic: a PVC with a volume still attached stays
        Terminating until the CSI driver releases it, so "delete returned"
        and "the name is free again" are different moments — and recreating
        the same name in between fails.
        """
        storage.validate()
        kube.kubectl(
            storage.kubeconfig_path, "delete", "pvc", claim.name,
            "-n", claim.namespace, "--ignore-not-found",
            error_cls=StorageError,
        )
        if wait:
            kube.wait_for_absent(
                storage.kubeconfig_path, "pvc", claim.name, claim.namespace,
                timeout=timeout, error_cls=StorageError,
            )
        print(f"[storage] claim {claim.namespace}/{claim.name} deleted")


_UNITS = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50,
          "Ei": 2**60, "k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12,
          "P": 10**15, "E": 10**18}


def _as_bytes(quantity: str) -> float:
    """A Kubernetes quantity in bytes, for comparing two sizes.

    Only needed to tell a grow from a shrink — string comparison would
    call '9Gi' larger than '10Gi'.
    """
    for suffix, factor in sorted(_UNITS.items(), key=lambda kv: -len(kv[0])):
        if quantity.endswith(suffix):
            return float(quantity[: -len(suffix)]) * factor
    return float(quantity)


__all__ = ["DRIVERS", "StorageBackend"]
