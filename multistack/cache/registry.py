"""What exists, and the backend that dispatches to it."""
from typing import Any, Dict, Optional

from ..capability import CapabilityBackend
from ..helm import HelmRelease
from ..state.tracking import track_create, track_delete, track_update
from .base import CacheError
from .spec import Cache

DRIVERS: Dict[str, Any] = {
    "valkey": ("multistack.cache.drivers.valkey", "ValkeyDriver"),
}


class CacheBackend(CapabilityBackend):
    CAPABILITY = "cache"
    DRIVERS = DRIVERS
    ERROR_CLS = CacheError

    def check_prerequisites(self, cache: Cache) -> None:
        return self.driver_for(cache).check_prerequisites(cache)

    # "cache", not "valkey": the component type names the capability a
    # dependent's REQUIRES asks for, not the implementation providing
    # it -- the same reason storage records "storage" and not "longhorn".
    @track_create("cache", name_of=lambda cache: cache.name)
    def create(self, cache: Cache) -> HelmRelease:
        return self.driver_for(cache).create(cache)

    @track_update(name_of=lambda cache: cache.name)
    def update(
        self,
        cache: Cache,
        values: Optional[Dict[str, Any]] = None,
        *,
        replace_values: bool = False,
    ) -> HelmRelease:
        return self.driver_for(cache).update(
            cache, values, replace_values=replace_values
        )

    @track_delete(name_of=lambda cache: cache.name)
    def delete(
        self,
        cache: Cache,
        delete_pvcs: bool = False,
        delete_namespace: bool = False,
    ) -> None:
        return self.driver_for(cache).delete(
            cache,
            delete_pvcs=delete_pvcs,
            delete_namespace=delete_namespace,
        )

    def exists(self, cache: Cache) -> bool:
        return self.driver_for(cache).exists(cache)

    def status(self, cache: Cache) -> HelmRelease:
        return self.driver_for(cache).status(cache)
