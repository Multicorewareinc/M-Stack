"""Shows the deployment-state progression for the full stack -- rke2 ->
longhorn -> minio -> vllm -> policy (rate limiter) -> gateway (model
gateway) -- exactly as recorded by @track_create (multistack/state/
tracking.py), which every real backend's create() is decorated with. No
backend, cluster, or kubectl here: this exercises the recording seam
itself with stand-in specs, the same way each real create() does it.
"""
from __future__ import annotations

import pytest

import multistack.state.tracking as tracking
from multistack.state import DependencyResolutionError, DeploymentStatus
from multistack.state.tracking import track_create, track_delete


class Spec:
    """Stands in for RKE2Cluster/Storage/MinIOTenant/etc: just the
    attributes track_create reads off a real spec."""

    def __init__(self, name, requires=()):
        self.name = name
        self.type = name
        self.REQUIRES = requires
        self.kubeconfig_path = "/tmp/kubeconfig"


# One decorated stand-in per component, component_type matching what the
# real backends use (see multistack/backends/*.py, */registry.py).
@track_create("cluster", name_of=lambda s: s.name)
def install_rke2(self, spec):
    return spec.name


@track_create("storage", name_of=lambda s: s.type)
def install_longhorn(self, spec):
    return spec.name


@track_create("objectstore", name_of=lambda s: s.name)
def install_minio(self, spec):
    return spec.name


@track_create("vllm", name_of=lambda s: s.name)
def install_vllm(self, spec):
    return spec.name


@track_create("policy", name_of=lambda s: s.type)
def install_policy(self, spec):
    return spec.name


@track_create("gateway", name_of=lambda s: s.type)
def install_gateway(self, spec):
    return spec.name


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """One throwaway SQLite file per test, and a reset of the
    process-wide singleton so it re-opens against that file rather than
    whatever an earlier test pointed it at."""
    monkeypatch.setenv("MULTISTACK_STATE_DB", str(tmp_path / "state.db"))
    tracking._default = None
    yield
    tracking._default = None


def test_full_stack_progresses_through_every_component_in_order(isolated_state):
    dummy = object()
    install_rke2(dummy, Spec("ai-cluster"))
    install_longhorn(dummy, Spec("longhorn", requires=("cluster",)))
    install_minio(dummy, Spec("minio-test", requires=("cluster", "storage")))
    install_vllm(dummy, Spec("vllm-qwen", requires=("cluster",)))
    install_policy(dummy, Spec("rpm", requires=("cluster",)))
    install_gateway(dummy, Spec("modelgateway", requires=("cluster",)))

    state = tracking.default_state_manager()
    deployments = {d.name: d for d in state.list_deployments()}

    assert set(deployments) == {
        "ai-cluster", "longhorn", "minio-test", "vllm-qwen", "rpm", "modelgateway",
    }
    assert all(d.status == DeploymentStatus.PROVISIONED for d in deployments.values())
    assert deployments["ai-cluster"].component_type == "cluster"
    assert deployments["longhorn"].component_type == "storage"
    assert deployments["minio-test"].component_type == "objectstore"
    assert deployments["vllm-qwen"].component_type == "vllm"
    assert deployments["rpm"].component_type == "policy"
    assert deployments["modelgateway"].component_type == "gateway"
    # Every install above shared one connection to one file.
    assert tracking.default_state_manager() is state


def test_a_component_started_out_of_order_is_rejected(isolated_state):
    dummy = object()
    install_rke2(dummy, Spec("ai-cluster"))
    # storage was never installed, so minio's "storage" requirement fails
    # before install_minio's own body ever runs.
    with pytest.raises(DependencyResolutionError, match="storage"):
        install_minio(dummy, Spec("minio-test", requires=("cluster", "storage")))
    assert tracking.default_state_manager().get("minio-test") is None


def test_a_failed_component_is_recorded_and_blocks_dependents(isolated_state):
    dummy = object()

    @track_create("cluster", name_of=lambda s: s.name)
    def install_broken_cluster(self, spec):
        raise RuntimeError("ssh unreachable")

    with pytest.raises(RuntimeError, match="ssh unreachable"):
        install_broken_cluster(dummy, Spec("ai-cluster"))

    state = tracking.default_state_manager()
    deployment = state.get("ai-cluster")
    assert deployment.status == DeploymentStatus.FAILED
    assert deployment.error == "ssh unreachable"

    with pytest.raises(DependencyResolutionError, match="cluster"):
        install_longhorn(dummy, Spec("longhorn", requires=("cluster",)))


def test_a_successful_delete_drops_the_row(isolated_state):
    dummy = object()

    @track_delete(name_of=lambda s: s.name)
    def remove_cluster(self, spec):
        return "removed"

    install_rke2(dummy, Spec("ai-cluster"))
    assert remove_cluster(dummy, Spec("ai-cluster")) == "removed"
    assert tracking.default_state_manager().get("ai-cluster") is None


def test_a_failed_delete_is_recorded_not_dropped(isolated_state):
    dummy = object()

    @track_delete(name_of=lambda s: s.name)
    def remove_cluster(self, spec):
        raise RuntimeError("node unreachable")

    install_rke2(dummy, Spec("ai-cluster"))
    with pytest.raises(RuntimeError, match="node unreachable"):
        remove_cluster(dummy, Spec("ai-cluster"))

    deployment = tracking.default_state_manager().get("ai-cluster")
    assert deployment.status == DeploymentStatus.FAILED
    assert deployment.error == "node unreachable"


def test_deleting_a_name_this_layer_never_tracked_is_untouched(isolated_state):
    """A name this process never recorded (created before this layer
    existed, or never tracked at all) still deletes -- track_delete only
    records state for rows that already exist, it never invents one."""
    dummy = object()

    @track_delete(name_of=lambda s: s.name)
    def remove_cluster(self, spec):
        return "removed"

    assert remove_cluster(dummy, Spec("untracked-cluster")) == "removed"
    assert tracking.default_state_manager().get("untracked-cluster") is None
