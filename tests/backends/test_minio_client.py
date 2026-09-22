"""Tests for MinIOBackend: prerequisite checks, kubeconfig scoping, the
helm-repo ordering fix, and PVC safety rules. Nothing shells out."""
import json
import subprocess

import pytest

from multistack import MinIOTenant
from multistack.state import tracking
from multistack.backends.minio_client import (
    MinIOBackend,
    MinIOPrerequisiteError,
    MinIOStorageError,
)

KUBECONFIG = "/tmp/kc.yaml"


@pytest.fixture
def backend():
    # A MinIO tenant declares REQUIRES = ("cluster", "storage"), and
    # @track_create refuses to run until the state layer holds a healthy
    # row for each. Seeded here rather than per-test because a MinIO
    # backend only ever exists on a cluster that already has storage --
    # an unseeded state DB is not a case worth testing through this
    # fixture. The live require_* checks are unaffected and are still
    # exercised by the prerequisite tests below.
    state = tracking.default_state_manager()
    for name, component in (("test-cluster", "cluster"),
                            ("test-storage", "storage")):
        state.start(name, component_type=component)
        state.mark_healthy(name)
    return MinIOBackend()


def tenant(**kwargs) -> MinIOTenant:
    defaults = dict(
        kubeconfig_path=KUBECONFIG, name="minio-test",
        namespace="minio-test", storage_class="longhorn",
    )
    return MinIOTenant(**{**defaults, **kwargs})


def stub_cli(backend, monkeypatch, responses=None, record=None):
    """Intercept every subprocess call, returning canned stdout by keyword."""
    responses = responses or {}

    def fake_local(argv, check=True):
        if record is not None:
            record.append(argv)
        joined = " ".join(argv)
        for needle, reply in responses.items():
            if needle in joined:
                return reply
        return ""

    monkeypatch.setattr(backend, "_local", fake_local)
    monkeypatch.setattr(backend, "_require_cli", lambda name: None)
    # The backend now verifies its declared cluster dependency for real.
    # These tests stub the transport, so there is no cluster to reach;
    # tests/test_capability.py covers require_cluster itself.
    monkeypatch.setattr(
        "multistack.backends.minio_client.require_cluster",
        lambda path, capability="?", **k: "stubbed",
    )


SC_LONGHORN = "longhorn\nlonghorn-static\n"


# -- prerequisites --------------------------------------------------------
def test_missing_named_storage_class_is_fatal(backend, monkeypatch):
    # The PVCs would sit Pending forever with nothing to provision them.
    stub_cli(backend, monkeypatch, {"get storageclass": "standard\n"})
    with pytest.raises(MinIOPrerequisiteError, match="does not exist"):
        backend.check_prerequisites(tenant(storage_class="longhorn"))


def test_cluster_with_no_storage_class_at_all_is_fatal(backend, monkeypatch):
    # This is what a fresh RKE2 cluster looks like — it ships no provisioner.
    stub_cli(backend, monkeypatch, {"get storageclass": ""})
    with pytest.raises(MinIOPrerequisiteError, match="no StorageClass at all"):
        backend.check_prerequisites(tenant(storage_class=None))


def test_no_default_storage_class_warns_when_none_named(backend, monkeypatch):
    # The default-class query is a separate, more specific jsonpath; an
    # empty answer means no class carries the is-default-class annotation.
    stub_cli(backend, monkeypatch, {
        "is-default-class": "",
        "get storageclass": SC_LONGHORN,
    })
    warnings = backend.check_prerequisites(tenant(storage_class=None))
    assert any("no default StorageClass" in w for w in warnings)


def test_named_storage_class_that_exists_passes(backend, monkeypatch):
    stub_cli(backend, monkeypatch, {"get storageclass": SC_LONGHORN})
    assert not any("StorageClass" in w for w in backend.check_prerequisites(tenant()))


def test_standalone_mode_warns_about_redundancy(backend, monkeypatch):
    stub_cli(backend, monkeypatch, {"get storageclass": SC_LONGHORN})
    warnings = backend.check_prerequisites(
        tenant(mode="standalone", servers=1, volumes_per_server=1)
    )
    assert any("no redundancy" in w for w in warnings)


# -- chart resolution -----------------------------------------------------
def test_the_minio_repo_is_reachable_without_the_backend_adding_it():
    """The backend used to run `helm repo add` itself, and the bug that
    guarded was adding it only while installing the operator -- which is
    cluster-scoped, so the second tenant skipped it and died with `repo
    minio not found`.

    The pyhelm port moved that out: charts are named bare (`operator`,
    `tenant`) and resolved against the Helm layer's shared repository set,
    so no backend registers anything. This asserts the replacement is
    actually in place, because a bare chart name with no matching
    repository fails at install time with nothing pointing back here.
    """
    from multistack.helm.repositories import DEFAULT_REPOSITORIES

    urls = {r.url.rstrip("/") for r in DEFAULT_REPOSITORIES}
    assert "https://operator.min.io" in urls, (
        "the MinIO operator repo is not in DEFAULT_REPOSITORIES, so the "
        "bare chart names below cannot resolve"
    )
    t = tenant()
    assert (t.operator_chart, t.tenant_chart) == ("operator", "tenant")


def test_both_charts_are_installed_into_their_own_namespaces(backend, stub_helm, monkeypatch):
    """The operator is cluster-scoped and the tenant is not, so they are two
    releases in two namespaces -- installing the tenant into the operator's
    namespace is a mistake nothing else catches."""
    runner = stub_helm(backend)
    stub_cli(backend, monkeypatch, {"get storageclass": SC_LONGHORN})

    backend.create(tenant(), wait_for_ready=False)

    by_release = {c["release"]: c for c in runner.installs}
    assert by_release["minio-operator"]["namespace"] == "minio-operator"
    assert by_release["minio-operator"]["chart"] == "operator"
    assert by_release["minio-test"]["namespace"] == "minio-test"
    assert by_release["minio-test"]["chart"] == "tenant"


def test_an_existing_operator_is_not_reinstalled(backend, stub_helm, monkeypatch):
    """It is cluster-scoped: the second tenant on a cluster must leave the
    operator alone rather than upgrade it underneath the first."""
    runner = stub_helm(backend)
    runner.given_deployed("minio-operator", namespace="minio-operator")
    stub_cli(backend, monkeypatch, {"get storageclass": SC_LONGHORN})

    backend.create(tenant(), wait_for_ready=False)

    assert runner.installed("minio-operator") == []
    assert len(runner.installed("minio-test")) == 1


# -- kubeconfig scoping ---------------------------------------------------
def test_every_helm_and_kubectl_call_is_kubeconfig_scoped(backend, monkeypatch):
    argvs = []

    def fake_run(argv, **kwargs):
        argvs.append(argv)
        stdout = SC_LONGHORN if "storageclass" in " ".join(argv) else "[]"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    # This one patches subprocess directly rather than going through
    # stub_cli, so the cluster check needs stubbing here too. It is a real
    # call now: the tenant declares REQUIRES = ("cluster", "storage").
    monkeypatch.setattr(
        "multistack.backends.minio_client.require_cluster",
        lambda path, capability="?", **k: "stubbed",
    )
    t = tenant()
    backend.check_prerequisites(t)
    backend.list_pvcs(t)

    # The CLI presence probes are `helm version` / `kubectl version
    # --client`; they talk to no cluster and take no kubeconfig.
    kube_calls = [
        a for a in argvs if a[0] in ("helm", "kubectl") and a[1] != "version"
    ]
    assert kube_calls
    for argv in kube_calls:
        assert argv[argv.index("--kubeconfig") + 1] == KUBECONFIG


# -- storage safety -------------------------------------------------------
def test_shrinking_a_pvc_is_refused_before_any_api_call(backend, monkeypatch):
    # Kubernetes cannot shrink a PVC, and its own error is far less clear.
    calls = []
    stub_cli(backend, monkeypatch, {
        "get pvc": "data0-minio-test-pool-0-0 30Gi\n",
    }, record=calls)

    with pytest.raises(MinIOStorageError, match="Cannot shrink"):
        backend.resize_storage(tenant(), "10Gi")
    assert not any("patch" in " ".join(c) for c in calls)


def test_growing_a_pvc_patches_every_one_and_updates_the_spec(backend, monkeypatch):
    calls = []
    stub_cli(backend, monkeypatch, {
        "get pvc": "data0-minio-test-pool-0-0 30Gi\ndata0-minio-test-pool-0-1 30Gi\n",
    }, record=calls)

    t = tenant(volume_size="30Gi")
    resized = backend.resize_storage(t, "50Gi")

    assert len(resized) == 2
    assert sum("patch" in " ".join(c) for c in calls) == 2
    # Kept in sync, so a later create() doesn't set the pool back.
    assert t.volume_size == "50Gi"


def test_resize_with_no_pvcs_is_an_error_not_a_silent_noop(backend, monkeypatch):
    stub_cli(backend, monkeypatch, {"get pvc": ""})
    with pytest.raises(MinIOStorageError, match="No PVCs found"):
        backend.resize_storage(tenant(), "50Gi")


def test_delete_keeps_pvcs_unless_asked(backend, stub_helm, monkeypatch):
    calls = []
    runner = stub_helm(backend)
    runner.given_deployed("minio-test", namespace="minio-test")
    stub_cli(backend, monkeypatch, {
        "get pvc": "data0-minio-test-pool-0-0\n",
    }, record=calls)

    backend.delete(tenant())
    assert runner.uninstalls[-1]["release"] == "minio-test"
    assert not any("delete pvc" in " ".join(c) for c in calls)

    calls.clear()
    runner.given_deployed("minio-test", namespace="minio-test")
    backend.delete(tenant(), delete_pvcs=True)
    assert any("delete pvc" in " ".join(c) for c in calls)


# -- credentials ----------------------------------------------------------
def test_create_returns_generated_credentials(backend, stub_helm, monkeypatch):
    stub_helm(backend)          # no release recorded => a fresh tenant
    stub_cli(backend, monkeypatch, {"get storageclass": SC_LONGHORN})
    info = backend.create(tenant(), wait_for_ready=False)
    assert len(info.root_user) == 16 and len(info.root_password) == 24
    assert info.endpoint == "https://minio.minio-test.svc.cluster.local"


def test_values_file_is_private_and_removed(backend, monkeypatch, tmp_path):
    # It holds the root credentials in cleartext.
    import os
    t = tenant()
    t.ensure_credentials()
    path = backend._write_values(t)
    try:
        assert oct(os.stat(path).st_mode)[-3:] == "600"
        assert json.load(open(path))["tenant"]["configSecret"]["accessKey"] == t.root_user
    finally:
        os.unlink(path)


# -- credential adoption on an existing tenant ----------------------------
import base64

EXISTING_SECRET = json.dumps({"data": {"config.env": base64.b64encode(
    b'export MINIO_ROOT_USER="admin"\nexport MINIO_ROOT_PASSWORD="admin123456"\n'
).decode()}})

DEPLOYED = json.dumps(
    [{"name": "minio-test", "namespace": "minio-test", "status": "deployed"}]
)


def test_existing_tenant_credentials_are_reused_not_regenerated(backend, stub_helm, monkeypatch):
    # Regenerating would rotate the live tenant's root credentials on the
    # next helm upgrade, breaking every client already using them.
    stub_helm(backend).given_deployed("minio-test", namespace="minio-test")
    stub_cli(backend, monkeypatch, {
        "get storageclass": SC_LONGHORN,
        "get secret": EXISTING_SECRET,
    })
    info = backend.create(tenant(), wait_for_ready=False)
    assert (info.root_user, info.root_password) == ("admin", "admin123456")


def test_unreadable_credentials_on_an_existing_tenant_is_an_error(backend, stub_helm, monkeypatch):
    # Silently minting new ones here is the dangerous outcome, so refuse.
    stub_helm(backend).given_deployed("minio-test", namespace="minio-test")
    stub_cli(backend, monkeypatch, {
        "get storageclass": SC_LONGHORN,
        "get secret": "",
    })
    with pytest.raises(Exception, match="could not be read"):
        backend.create(tenant(), wait_for_ready=False)


def test_explicit_credentials_are_honoured_over_the_existing_secret(backend, stub_helm, monkeypatch):
    stub_helm(backend).given_deployed("minio-test", namespace="minio-test")
    stub_cli(backend, monkeypatch, {
        "get storageclass": SC_LONGHORN,
        "get secret": EXISTING_SECRET,
    })
    info = backend.create(
        tenant(root_user="new", root_password="rotate-me"), wait_for_ready=False
    )
    assert info.root_user == "new"


def test_new_tenant_still_generates_credentials(backend, stub_helm, monkeypatch):
    stub_helm(backend)
    stub_cli(backend, monkeypatch, {"get storageclass": SC_LONGHORN})
    info = backend.create(tenant(), wait_for_ready=False)
    assert len(info.root_user) == 16


# -- storage inspection ---------------------------------------------------
def test_endpoint_is_the_in_cluster_s3_url(backend):
    """What Inference.s3_endpoint_url is handed. https because the tenant
    requests an auto-cert by default."""
    assert backend.endpoint(tenant()) == "https://minio.minio-test.svc.cluster.local"
    assert backend.endpoint(tenant(request_auto_cert=False)).startswith("http://")


def test_pvc_sizes_pairs_every_claim_with_its_request(backend, monkeypatch):
    stub_cli(backend, monkeypatch, {
        "get pvc": "data0-minio-test-pool-0-0 30Gi\n"
                   "data1-minio-test-pool-0-0 30Gi\n"
                   "data0-minio-test-pool-0-1 30Gi\n",
    })
    sizes = backend.pvc_sizes(tenant())
    assert len(sizes) == 3
    assert sizes["data0-minio-test-pool-0-1"] == "30Gi"


def test_pvc_sizes_survives_a_claim_with_no_size(backend, monkeypatch):
    """A PVC still being provisioned has no requests.storage yet, and a
    ragged line must not take the whole listing down."""
    stub_cli(backend, monkeypatch, {
        "get pvc": "data0-minio-test-pool-0-0 30Gi\ndata1-minio-test-pool-0-0\n",
    })
    sizes = backend.pvc_sizes(tenant())
    assert sizes == {"data0-minio-test-pool-0-0": "30Gi"}


# -- destructive ----------------------------------------------------------
def test_delete_pvcs_removes_every_claim_and_names_them(backend, monkeypatch):
    """Irreversible: the tenant's data goes with them. It returns the names
    so a caller can report what it destroyed rather than a count."""
    calls = []
    stub_cli(backend, monkeypatch, {
        "get pvc": "data0-minio-test-pool-0-0\ndata0-minio-test-pool-0-1\n",
    }, record=calls)

    removed = backend.delete_pvcs(tenant())

    assert removed == ["data0-minio-test-pool-0-0", "data0-minio-test-pool-0-1"]
    deletes = [" ".join(c) for c in calls if "delete pvc" in " ".join(c)]
    assert len(deletes) == 2
    # --ignore-not-found, so a partial teardown can be finished by re-running.
    assert all("--ignore-not-found" in d for d in deletes)


def test_delete_pvcs_on_a_tenant_with_none_is_not_an_error(backend, monkeypatch):
    stub_cli(backend, monkeypatch, {"get pvc": ""})
    assert backend.delete_pvcs(tenant()) == []


def test_destructive_storage_operations_are_recorded_in_state(backend, monkeypatch):
    """resize_storage and delete_pvcs change a live deployment. Before
    track_update they recorded nothing, so the row kept asserting the shape
    the tenant had when it was created."""
    state = tracking.default_state_manager()
    state.start("minio-test", component_type="objectstore")
    state.mark_healthy("minio-test")

    stub_cli(backend, monkeypatch, {
        "get pvc": "data0-minio-test-pool-0-0 30Gi\n",
    })
    backend.resize_storage(tenant(volume_size="30Gi"), "40Gi")
    # Still healthy -- an update restores the prior status rather than
    # asserting one of its own.
    assert state.get("minio-test").status.value == "provisioned"


def test_a_refused_resize_leaves_the_tenant_healthy(backend, monkeypatch):
    """A rejection is the SDK working, not the deployment breaking.

    Both MinIOStorageError paths out of resize_storage are pre-flight — a
    shrink, which Kubernetes cannot do, and no PVCs to resize — so the
    tenant is exactly as it was. Found live: refusing a 3Gi→1Gi shrink on
    a healthy probe tenant left its row reading `failed`.
    """
    state = tracking.default_state_manager()
    state.start("minio-test", component_type="objectstore")
    state.mark_healthy("minio-test")

    stub_cli(backend, monkeypatch, {"get pvc": ""})
    with pytest.raises(MinIOStorageError, match="No PVCs"):
        backend.resize_storage(tenant(), "40Gi")
    assert state.get("minio-test").status.value == "provisioned"

    stub_cli(backend, monkeypatch, {"get pvc": "data0-minio-test-pool-0-0 30Gi\n"})
    with pytest.raises(MinIOStorageError, match="Cannot shrink"):
        backend.resize_storage(tenant(volume_size="30Gi"), "10Gi")
    assert state.get("minio-test").status.value == "provisioned"


def test_an_update_that_breaks_partway_does_mark_failed(backend, monkeypatch):
    """The other half of the rule: only a refusal is exempt. Something that
    fails mid-flight may have applied half its changes, which is exactly
    the state someone needs to find."""
    state = tracking.default_state_manager()
    state.start("minio-test", component_type="objectstore")
    state.mark_healthy("minio-test")

    stub_cli(backend, monkeypatch, {"get pvc": "data0-minio-test-pool-0-0 30Gi\n"})
    monkeypatch.setattr(backend, "_kubectl", _explode)

    with pytest.raises(RuntimeError, match="apiserver went away"):
        backend.resize_storage(tenant(volume_size="30Gi"), "40Gi")

    row = state.get("minio-test")
    assert row.status.value == "failed" and "apiserver went away" in row.error


def _explode(*args, **kwargs):
    raise RuntimeError("apiserver went away")
