"""
The seam backends call into the state layer through — callers of a backend
never see StateManager.

Each capability's backend (RKE2Backend, StorageBackend, MinIOBackend,
InferenceBackend, IngressGatewayBackend today) decorates its own create() with
`@track_create(...)` below, and its own delete() with `@track_delete(...)`.
That's the only change a backend needs: `track_create` checks the spec's
own REQUIRES are healthy first, records PROVISIONING before the real work
starts, then HEALTHY on a clean return or FAILED (re-raising) on any
exception; `track_delete` mirrors that on the way out -- DELETING, then the
row is dropped entirely on success or marked FAILED on any exception. A
user script just calls `SomeBackend().create(spec)` / `.delete(spec)` like
it always did.

One StateManager per process, cached in `default_state_manager()`, so all
six backends record into the same SQLite file over one connection rather
than each opening it fresh -- correct either way (db.py runs WAL), but
wasteful otherwise. Deployments run serially in one process, so one
connection is all a run ever needs.
"""
from __future__ import annotations

import functools
import os
from typing import Any, Callable, Optional

from .config import StateConfig
from .manager import StateManager
from .models import DeploymentStatus

_default: Optional[StateManager] = None


def default_state_manager() -> StateManager:
    """The process-wide StateManager every backend records through.

    Points at MULTISTACK_STATE_DB if set, else StateConfig's own default
    (~/.multistack/state.db) -- same override pattern as
    MULTISTACK_KUBECONFIG elsewhere in this SDK. This is the one place
    that default is read from; see config.py for why it isn't rooted.
    """
    global _default
    if _default is None:
        db_path = os.environ.get("MULTISTACK_STATE_DB")
        config = StateConfig(db_path=os.path.expanduser(db_path)) if db_path else StateConfig()
        _default = StateManager(config)
    return _default


def track_create(component_type: str, name_of: Callable[[Any], str]):
    """Wraps a backend's `create(self, spec, ...)` with state recording.

    `name_of(spec)` gives the deployment's unique name -- `spec.name` for
    multi-instance specs (RKE2Cluster, MinIOTenant, Inference), or
    `spec.type` for the single-instance capability specs (Storage, Policy,
    Gateway) that have no name field of their own. `component_type` is the
    capability name a dependent spec's own REQUIRES tuple names (e.g.
    "cluster", "storage", "objectstore", "policy", "gateway") -- it has to
    match those strings for require_healthy_dependencies() to find this
    row.
    """
    def decorator(create_method):
        @functools.wraps(create_method)
        def wrapper(self, spec, *args, **kwargs):
            state = default_state_manager()
            name = name_of(spec)
            state.require_healthy_dependencies(getattr(spec, "REQUIRES", ()))
            state.start(name, component_type=component_type)
            try:
                result = create_method(self, spec, *args, **kwargs)
            except Exception as exc:
                state.mark_failed(name, str(exc))
                raise
            state.mark_healthy(name, kubeconfig_path=getattr(spec, "kubeconfig_path", None))
            return result
        return wrapper
    return decorator


def track_delete(name_of: Callable[[Any], str]):
    """Wraps a backend's `delete(self, spec_or_name, ...)` with state
    recording -- the `track_create` counterpart, so a component is no
    longer wired into this layer on the way in but invisible on the way
    out.

    `name_of` gets whatever `delete()`'s first argument actually is:
    a full spec for most backends, but `RKE2Backend.delete` also accepts a
    bare cluster name, so `name_of` has to handle both (see its call
    sites for the `isinstance(x, str)` check that does).

    Only touches state for a name this layer already has a row for --
    deleting something this process never tracked (a name typo, a
    deployment created before this layer existed) runs the real
    delete() untouched rather than raising a spurious not-found from
    here. On success the row is removed entirely: once torn down, there
    is nothing left to call `"healthy"` or `"deleted"`, and `remove()` is
    exactly the operation for "the real work already succeeded, drop the
    record" (see `StateManager.remove`). On failure the row is kept and
    marked FAILED with the exception text, same as `track_create` -- a
    delete that raised may have left things partly torn down, which is
    not the same as "gone".
    """
    def decorator(delete_method):
        @functools.wraps(delete_method)
        def wrapper(self, spec, *args, **kwargs):
            state = default_state_manager()
            name = name_of(spec)
            tracked = state.get(name) is not None
            if tracked:
                state.set_status(name, DeploymentStatus.DELETING)
            try:
                result = delete_method(self, spec, *args, **kwargs)
            except Exception as exc:
                if tracked:
                    state.mark_failed(name, str(exc))
                raise
            if tracked:
                state.remove(name)
            return result
        return wrapper
    return decorator


def track_update(
    name_of: Callable[[Any], str],
    refused: tuple = (),
):
    """Wraps a backend method that *changes* an existing deployment.

    The third verb. `track_create` records that a deployment came into
    being and `track_delete` that it went away, but everything between --
    adding a node to a cluster, growing a tenant's volumes, resizing a
    claim -- ran with no record at all. The row kept saying `healthy`
    from whenever the create last succeeded, which is true and useless:
    `ai-cluster` read the same whether it had three nodes or five.

    Differs from `track_create` in three ways, each for a reason:

    * **It does not check REQUIRES.** Dependencies were satisfied when
      the thing was created; re-checking would refuse to resize a tenant
      because some *other* capability is currently unhealthy.
    * **It does not create a row.** Updating something this layer never
      recorded runs the real method untouched, the same courtesy
      `track_delete` extends -- an update is not the place to invent a
      deployment's history.
    * **It restores the previous status on success**, rather than
      asserting HEALTHY. A resize that succeeds against a DEGRADED
      deployment has not made it healthy, and saying so would be a
      louder lie than saying nothing.

    On failure the row is marked FAILED with the exception text, because
    a half-applied update is exactly the state someone needs to find.

    `refused` names the exceptions that mean the opposite: the request was
    rejected before anything was touched, so the deployment is exactly as
    it was and its status should say so. Found live -- asking MinIO to
    shrink a PVC is refused pre-flight (Kubernetes cannot shrink one, and
    its own error is far less clear), and without this a correctly
    rejected request left a healthy tenant reading `failed`. A refusal is
    the SDK working, not the deployment breaking.
    """
    def decorator(update_method):
        @functools.wraps(update_method)
        def wrapper(self, spec, *args, **kwargs):
            state = default_state_manager()
            name = name_of(spec)
            existing = state.get(name)
            if existing is not None:
                previous = existing.status
                state.set_status(name, DeploymentStatus.PROVISIONING)
            try:
                result = update_method(self, spec, *args, **kwargs)
            except refused:
                # Rejected before the cluster was touched: restore the
                # status and let the exception through unchanged.
                if existing is not None:
                    state.set_status(name, previous)
                raise
            except Exception as exc:
                if existing is not None:
                    state.mark_failed(name, str(exc))
                raise
            if existing is not None:
                state.set_status(name, previous)
            return result
        return wrapper
    return decorator


__all__ = [
    "default_state_manager", "track_create", "track_delete", "track_update",
]
