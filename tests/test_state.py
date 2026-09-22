"""Tests for the platform state layer: SQLite-backed lifecycle tracking.

StateManager is a pure recorder -- it never provisions anything itself, so
none of this touches ssh, kubectl, or a real cluster. Whatever performs the
real work (an RKE2Backend.create()/update()/delete() call, say) is expected
to report into it as it goes; see the module docstring in
multistack/state/__init__.py for that call shape."""
from __future__ import annotations

import pytest

from multistack.state import (
    DependencyResolutionError,
    DeploymentNotFoundError,
    DeploymentStatus,
    HealthStatus,
    StateConfig,
    StateManager,
)


def make_manager(tmp_path):
    config = StateConfig(db_path=str(tmp_path / "state.db"))
    return StateManager(config)


def test_start_records_a_new_deployment_as_provisioning(tmp_path):
    manager = make_manager(tmp_path)

    deployment = manager.start("test-cluster", component_type="rke2")

    assert deployment.component_type == "rke2"
    assert deployment.status == DeploymentStatus.PROVISIONING


def test_start_again_reuses_the_existing_row(tmp_path):
    manager = make_manager(tmp_path)
    manager.start("test-cluster", component_type="rke2")

    manager.start("test-cluster", component_type="rke2")

    assert [d.name for d in manager.list_deployments()] == ["test-cluster"]


def test_mark_healthy_records_status_kubeconfig_and_health_check(tmp_path):
    manager = make_manager(tmp_path)
    manager.start("test-cluster", component_type="rke2")

    manager.mark_healthy("test-cluster", kubeconfig_path="/tmp/kubeconfig")

    deployment = manager.get("test-cluster")
    assert deployment.status == DeploymentStatus.PROVISIONED
    assert deployment.kubeconfig_path == "/tmp/kubeconfig"
    assert manager.latest_health_check("test-cluster").status == HealthStatus.HEALTHY


def test_mark_failed_records_error_and_health_check(tmp_path):
    manager = make_manager(tmp_path)
    manager.start("test-cluster", component_type="rke2")

    manager.mark_failed("test-cluster", "ssh unreachable")

    deployment = manager.get("test-cluster")
    assert deployment.status == DeploymentStatus.FAILED
    assert deployment.error == "ssh unreachable"
    assert manager.latest_health_check("test-cluster").status == HealthStatus.UNHEALTHY


def test_mark_degraded_records_status_and_health_check(tmp_path):
    manager = make_manager(tmp_path)
    manager.start("test-cluster", component_type="rke2")

    manager.mark_degraded("test-cluster", "1 of 3 nodes not ready")

    deployment = manager.get("test-cluster")
    assert deployment.status == DeploymentStatus.DEGRADED
    assert manager.latest_health_check("test-cluster").status == HealthStatus.DEGRADED


def test_record_health_check_unknown_leaves_status_alone(tmp_path):
    manager = make_manager(tmp_path)
    manager.start("test-cluster", component_type="rke2")
    manager.mark_healthy("test-cluster")

    manager.record_health_check("test-cluster", HealthStatus.UNKNOWN, "no kubeconfig")

    assert manager.get("test-cluster").status == DeploymentStatus.PROVISIONED
    assert manager.latest_health_check("test-cluster").status == HealthStatus.UNKNOWN


def test_remove_deletes_the_record(tmp_path):
    manager = make_manager(tmp_path)
    manager.start("test-cluster", component_type="rke2")

    manager.remove("test-cluster")

    assert manager.get("test-cluster") is None


def test_remove_unknown_deployment_raises(tmp_path):
    manager = make_manager(tmp_path)

    with pytest.raises(DeploymentNotFoundError):
        manager.remove("nope")


def test_require_healthy_dependencies_blocks_an_unmet_requirement(tmp_path):
    manager = make_manager(tmp_path)

    with pytest.raises(DependencyResolutionError, match="storage"):
        manager.require_healthy_dependencies(["storage"])


def test_require_healthy_dependencies_passes_once_the_dependency_is_healthy(tmp_path):
    manager = make_manager(tmp_path)
    manager.start("storage-a", component_type="storage")
    manager.mark_healthy("storage-a")

    manager.require_healthy_dependencies(["storage"])


def test_list_deployments_returns_everything_recorded(tmp_path):
    manager = make_manager(tmp_path)
    manager.start("cluster-a", component_type="rke2")
    manager.start("cluster-b", component_type="storage")

    names = {d.name for d in manager.list_deployments()}

    assert names == {"cluster-a", "cluster-b"}


def test_a_renamed_capability_updates_the_recorded_component_type(tmp_path):
    """start() used to set component_type only when creating the row, so a
    capability rename left old rows behind under the old name.

    vLLM became the `inference` capability. The live vllm-qwen row said
    "vllm" while the new code wrote "inference", which is one capability
    under two names — and has_healthy("inference") could not see a
    deployment that was plainly there, so a future
    REQUIRES=("inference",) would have refused to build on it.
    """
    manager = make_manager(tmp_path)
    manager.start("vllm-qwen", component_type="vllm")
    manager.mark_healthy("vllm-qwen")
    assert manager.get("vllm-qwen").component_type == "vllm"

    # The same deployment, re-run after the rename.
    manager.start("vllm-qwen", component_type="inference")
    assert manager.get("vllm-qwen").component_type == "inference"

    manager.mark_healthy("vllm-qwen")
    manager.require_healthy_dependencies(("inference",))

    # And the old name no longer resolves, which is the point: there is
    # one row, not two, and it says what the code says.
    with pytest.raises(DependencyResolutionError):
        manager.require_healthy_dependencies(("vllm",))


# -- track_update ---------------------------------------------------------
def _tracked_backend(status=None):
    """A stand-in backend with an update method wrapped by track_update."""
    from multistack.state.tracking import track_update

    class Spec:
        name = "thing"
        REQUIRES = ("cluster",)

    class Backend:
        def __init__(self):
            self.ran = 0

        @track_update(name_of=lambda spec: spec.name)
        def update(self, spec, fail=False):
            self.ran += 1
            if fail:
                raise RuntimeError("update blew up")
            return "done"

    return Backend(), Spec()


def test_update_keeps_the_row_and_restores_its_status(tmp_path):
    """An update is not a create: it must not assert HEALTHY on a
    deployment that was DEGRADED before it started. Succeeding at a resize
    does not make an unhealthy tenant healthy."""
    manager = make_manager(tmp_path)
    backend, spec = _tracked_backend()
    manager.start("thing", component_type="objectstore")
    manager.set_status("thing", DeploymentStatus.DEGRADED)

    assert backend.update(spec) == "done"

    assert manager.get("thing").status is DeploymentStatus.DEGRADED


def test_update_marks_failed_and_re_raises(tmp_path):
    """A half-applied update is exactly the state someone needs to find."""
    manager = make_manager(tmp_path)
    backend, spec = _tracked_backend()
    manager.start("thing", component_type="objectstore")
    manager.mark_healthy("thing")

    with pytest.raises(RuntimeError, match="update blew up"):
        backend.update(spec, fail=True)

    row = manager.get("thing")
    assert row.status is DeploymentStatus.FAILED
    assert "update blew up" in row.error


def test_update_of_an_untracked_deployment_runs_untouched(tmp_path):
    """Updating something this layer never recorded is not the place to
    invent its history -- the same courtesy track_delete extends."""
    make_manager(tmp_path)
    backend, spec = _tracked_backend()

    assert backend.update(spec) == "done"
    assert backend.ran == 1


def test_update_does_not_recheck_dependencies(tmp_path):
    """Dependencies were satisfied when the thing was created. Re-checking
    would refuse to resize a tenant because some unrelated capability is
    unhealthy right now."""
    manager = make_manager(tmp_path)
    backend, spec = _tracked_backend()
    manager.start("thing", component_type="objectstore")
    manager.mark_healthy("thing")
    # Nothing provides "cluster", which spec.REQUIRES names.
    with pytest.raises(DependencyResolutionError):
        manager.require_healthy_dependencies(spec.REQUIRES)

    assert backend.update(spec) == "done"
    assert manager.get("thing").status is DeploymentStatus.PROVISIONED


def test_update_distinguishes_a_refusal_from_a_failure(tmp_path):
    """A request rejected before anything was touched leaves the
    deployment exactly as it was, so its status should say so. Without
    this, refusing an invalid resize on a healthy tenant made the tenant
    read `failed` — the SDK working correctly, recorded as a breakage."""
    from multistack.state.tracking import track_update

    class Refused(Exception):
        pass

    class Spec:
        name = "thing"

    class Backend:
        @track_update(name_of=lambda s: s.name, refused=(Refused,))
        def update(self, spec, how):
            raise Refused("shrink") if how == "refuse" else RuntimeError("broke")

    manager = make_manager(tmp_path)
    manager.start("thing", component_type="objectstore")
    manager.mark_healthy("thing")
    backend, spec = Backend(), Spec()

    with pytest.raises(Refused):
        backend.update(spec, "refuse")
    assert manager.get("thing").status is DeploymentStatus.PROVISIONED

    with pytest.raises(RuntimeError):
        backend.update(spec, "break")
    assert manager.get("thing").status is DeploymentStatus.FAILED
