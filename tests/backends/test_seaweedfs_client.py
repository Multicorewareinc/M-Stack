"""Tests for SeaweedFSBackend: prerequisite checks, kubeconfig scoping,
credential adoption, and PVC safety rules. Nothing shells out."""
import base64
import json
import subprocess

import pytest

from multistack import SeaweedFSCluster
from multistack.state import tracking
from multistack.backends.seaweedfs_client import (
    SeaweedFSBackend,
    SeaweedFSPrerequisiteError,
    SeaweedFSStorageError,
)

KUBECONFIG = "/tmp/kc.yaml"


@pytest.fixture
def backend():
    # A SeaweedFSCluster declares REQUIRES = ("cluster", "storage"), and
    # @track_create refuses to run until the state layer holds a healthy
    # row for each. Seeded here rather than per-test because a SeaweedFS
    # backend only ever exists on a cluster that already has storage --
    # an unseeded state DB is not a case worth testing through this
    # fixture. The live require_* checks are unaffected and are still
    # exercised by the prerequisite tests below.
    state = tracking.default_state_manager()
    for name, component in (("test-cluster", "cluster"),
                            ("test-storage", "storage")):
        state.start(name, component_type=component)
        state.mark_healthy(name)
    return SeaweedFSBackend()


def cluster(**kwargs) -> SeaweedFSCluster:
    defaults = dict(
        kubeconfig_path=KUBECONFIG, name="seaweedfs-test",
        namespace="seaweedfs-test", storage_class="longhorn",
    )
    return SeaweedFSCluster(**{**defaults, **kwargs})


def stub_cli(backend, monkeypatch, responses=None, record=None):
    """Intercept every subprocess call, returning canned stdout by keyword.

    `kube_apply` (used for the config Secret, not `_kubectl`) is a
    separate seam -- it calls `multistack.kube.apply` directly rather
    than going through `backend._local`, so it needs its own stub here,
    the same way the NetworkPolicy apply did for MinIOBackend.
    """
    responses = responses or {}

    def fake_local(argv, check=True):
        if record is not None:
            record.append(argv)
        joined = " ".join(argv)
        for needle, reply in responses.items():
            if needle in joined:
                return reply
        return ""

    def fake_kube_apply(kubeconfig_path, manifest, *, error_cls=None, **kwargs):
        if record is not None:
            record.append(("kube_apply", manifest))
        return ""

    monkeypatch.setattr(backend, "_local", fake_local)
    monkeypatch.setattr(backend, "_require_cli", lambda name: None)
    # The backend now verifies its declared cluster dependency for real.
    # These tests stub the transport, so there is no cluster to reach;
    # tests/test_capability.py covers require_cluster itself.
    monkeypatch.setattr(
        "multistack.backends.seaweedfs_client.require_cluster",
        lambda path, capability="?", **k: "stubbed",
    )
    monkeypatch.setattr(
        "multistack.backends.seaweedfs_client.kube_apply", fake_kube_apply,
    )


SC_LONGHORN = "longhorn\nlonghorn-static\n"


# -- prerequisites --------------------------------------------------------
def test_missing_named_storage_class_is_fatal(backend, monkeypatch):
    # The PVCs would sit Pending forever with nothing to provision them.
    stub_cli(backend, monkeypatch, {"get storageclass": "standard\n"})
    with pytest.raises(SeaweedFSPrerequisiteError, match="does not exist"):
        backend.check_prerequisites(cluster(storage_class="longhorn"))


def test_cluster_with_no_storage_class_at_all_is_fatal(backend, monkeypatch):
    # This is what a fresh RKE2 cluster looks like -- it ships no provisioner.
    stub_cli(backend, monkeypatch, {"get storageclass": ""})
    with pytest.raises(SeaweedFSPrerequisiteError, match="no StorageClass at all"):
        backend.check_prerequisites(cluster(storage_class=None))


def test_no_default_storage_class_warns_when_none_named(backend, monkeypatch):
    stub_cli(backend, monkeypatch, {
        "is-default-class": "",
        "get storageclass": SC_LONGHORN,
    })
    warnings = backend.check_prerequisites(cluster(storage_class=None))
    assert any("no default StorageClass" in w for w in warnings)


def test_named_storage_class_that_exists_passes(backend, monkeypatch):
    stub_cli(backend, monkeypatch, {"get storageclass": SC_LONGHORN})
    assert not any("StorageClass" in w for w in backend.check_prerequisites(cluster()))


def test_default_replication_warns_about_no_redundancy(backend, monkeypatch):
    stub_cli(backend, monkeypatch, {"get storageclass": SC_LONGHORN})
    warnings = backend.check_prerequisites(cluster(replication="000"))
    assert any("no redundancy" in w for w in warnings)


def test_a_nonzero_replication_does_not_warn(backend, monkeypatch):
    stub_cli(backend, monkeypatch, {"get storageclass": SC_LONGHORN})
    warnings = backend.check_prerequisites(cluster(replication="001"))
    assert not any("redundancy" in w for w in warnings)


# -- chart resolution -----------------------------------------------------
def test_the_seaweedfs_repo_is_in_the_default_repositories():
    """Unlike MinIO's operator/tenant split, one Helm release deploys
    everything -- so there is exactly one chart to resolve, and it needs
    to be reachable through the shared repository set the same way every
    other chart-backed backend's does."""
    from multistack.helm.repositories import DEFAULT_REPOSITORIES

    urls = {r.url.rstrip("/") for r in DEFAULT_REPOSITORIES}
    assert "https://seaweedfs.github.io/seaweedfs/helm" in urls
    c = cluster()
    assert c.chart_name == "seaweedfs"


def test_a_single_release_is_installed_no_operator_step(backend, stub_helm, monkeypatch):
    """There is no cluster-scoped operator to install first -- create()
    is one install-or-upgrade, not an operator-then-tenant pair."""
    runner = stub_helm(backend)
    stub_cli(backend, monkeypatch, {"get storageclass": SC_LONGHORN})

    backend.create(cluster(), wait_for_ready=False)

    assert len(runner.installs) == 1
    install = runner.installs[0]
    assert install["release"] == "seaweedfs-test"
    assert install["namespace"] == "seaweedfs-test"
    assert install["chart"] == "seaweedfs"


def test_release_name_can_be_overridden(backend, stub_helm, monkeypatch):
    runner = stub_helm(backend)
    stub_cli(backend, monkeypatch, {"get storageclass": SC_LONGHORN})

    backend.create(cluster(release_name="mystack"), wait_for_ready=False)

    assert runner.installs[0]["release"] == "mystack"


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
    # call now: the cluster declares REQUIRES = ("cluster", "storage").
    monkeypatch.setattr(
        "multistack.backends.seaweedfs_client.require_cluster",
        lambda path, capability="?", **k: "stubbed",
    )
    c = cluster()
    backend.check_prerequisites(c)
    backend.list_pvcs(c)

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
        "get pvc": "data1-seaweedfs-test-volume-0 30Gi\n",
    }, record=calls)

    with pytest.raises(SeaweedFSStorageError, match="Cannot shrink"):
        backend.resize_storage(cluster(), "10Gi")
    assert not any(isinstance(c, list) and "patch" in " ".join(c) for c in calls)


def test_growing_a_pvc_patches_every_one_and_updates_the_spec(backend, monkeypatch):
    calls = []
    stub_cli(backend, monkeypatch, {
        "get pvc": "data1-seaweedfs-test-volume-0 30Gi\ndata1-seaweedfs-test-volume-1 30Gi\n",
    }, record=calls)

    c = cluster(volume_size="30Gi")
    resized = backend.resize_storage(c, "50Gi")

    assert len(resized) == 2
    assert sum(isinstance(x, list) and "patch" in " ".join(x) for x in calls) == 2
    # Kept in sync, so a later create() doesn't set the dataDir back.
    assert c.volume_size == "50Gi"


def test_resize_with_no_pvcs_is_an_error_not_a_silent_noop(backend, monkeypatch):
    stub_cli(backend, monkeypatch, {"get pvc": ""})
    with pytest.raises(SeaweedFSStorageError, match="No PVCs found"):
        backend.resize_storage(cluster(), "50Gi")


def test_delete_keeps_pvcs_and_secret_unless_asked(backend, stub_helm, monkeypatch):
    calls = []
    runner = stub_helm(backend)
    runner.given_deployed("seaweedfs-test", namespace="seaweedfs-test")
    stub_cli(backend, monkeypatch, {
        "get pvc": "data1-seaweedfs-test-volume-0\n",
    }, record=calls)

    backend.delete(cluster())
    assert runner.uninstalls[-1]["release"] == "seaweedfs-test"
    assert not any(isinstance(c, list) and "delete pvc" in " ".join(c) for c in calls)
    assert not any(isinstance(c, list) and "delete secret" in " ".join(c) for c in calls)

    calls.clear()
    runner.given_deployed("seaweedfs-test", namespace="seaweedfs-test")
    backend.delete(cluster(), delete_pvcs=True)
    assert any(isinstance(c, list) and "delete pvc" in " ".join(c) for c in calls)
    assert any(isinstance(c, list) and "delete secret" in " ".join(c) for c in calls)


# -- credentials ------------------------------------------------------------
def test_create_returns_generated_credentials(backend, stub_helm, monkeypatch):
    stub_helm(backend)          # no release recorded => a fresh cluster
    stub_cli(backend, monkeypatch, {"get storageclass": SC_LONGHORN})
    info = backend.create(cluster(), wait_for_ready=False)
    assert len(info.access_key) == 16 and len(info.secret_key) == 24
    assert info.endpoint == "http://seaweedfs-test-s3.seaweedfs-test.svc.cluster.local:8333"


def test_config_secret_is_applied_before_the_release(backend, stub_helm, monkeypatch):
    """s3.existingConfigSecret names it, so the Secret has to exist before
    the S3 gateway pod is scheduled -- applying it after the chart install
    would race the pod against the Secret."""
    calls = []
    stub_helm(backend)
    stub_cli(backend, monkeypatch, {"get storageclass": SC_LONGHORN}, record=calls)

    backend.create(cluster(), wait_for_ready=False)

    kinds = [c[1][0]["kind"] for c in calls if isinstance(c, tuple) and c[0] == "kube_apply"]
    secret_calls = [c for c in calls if isinstance(c, tuple) and c[0] == "kube_apply"]
    assert secret_calls, "kube_apply was never called for the config secret"
    objects = secret_calls[0][1]
    assert [o["kind"] for o in objects] == ["Namespace", "Secret"]
    assert objects[1]["metadata"]["name"] == cluster().config_secret_name
    assert "seaweedfs_s3_config" in objects[1]["stringData"]


def test_values_file_is_private_and_removed_and_carries_no_credentials(backend, monkeypatch):
    # Credentials live in the config Secret, not the Helm values -- but
    # the file is still private and cleaned up, matching every other
    # chart-backed backend's convention.
    import os
    c = cluster()
    c.ensure_credentials()
    path = backend._write_values(c)
    try:
        assert oct(os.stat(path).st_mode)[-3:] == "600"
        dumped = open(path).read()
        assert c.access_key not in dumped and c.secret_key not in dumped
        assert json.load(open(path))["s3"]["existingConfigSecret"] == c.config_secret_name
    finally:
        os.unlink(path)


def _existing_secret_for(access_key: str, secret_key: str) -> str:
    config = json.dumps({
        "identities": [{
            "name": "seaweedfs-test",
            "credentials": [{"accessKey": access_key, "secretKey": secret_key}],
            "actions": ["Admin", "Read", "Write"],
        }],
    })
    return json.dumps({"data": {"seaweedfs_s3_config": base64.b64encode(
        config.encode()
    ).decode()}})


EXISTING_SECRET = _existing_secret_for("admin", "admin123456")


def test_existing_cluster_credentials_are_reused_not_regenerated(backend, stub_helm, monkeypatch):
    # Regenerating would rotate the live cluster's credentials on the
    # next apply, breaking every client already using them.
    stub_helm(backend).given_deployed("seaweedfs-test", namespace="seaweedfs-test")
    stub_cli(backend, monkeypatch, {
        "get storageclass": SC_LONGHORN,
        "get secret": EXISTING_SECRET,
    })
    info = backend.create(cluster(), wait_for_ready=False)
    assert (info.access_key, info.secret_key) == ("admin", "admin123456")


def test_unreadable_credentials_on_an_existing_cluster_is_an_error(backend, stub_helm, monkeypatch):
    # Silently minting new ones here is the dangerous outcome, so refuse.
    stub_helm(backend).given_deployed("seaweedfs-test", namespace="seaweedfs-test")
    stub_cli(backend, monkeypatch, {
        "get storageclass": SC_LONGHORN,
        "get secret": "",
    })
    with pytest.raises(Exception, match="could not be read"):
        backend.create(cluster(), wait_for_ready=False)


def test_explicit_credentials_are_honoured_over_the_existing_secret(backend, stub_helm, monkeypatch):
    stub_helm(backend).given_deployed("seaweedfs-test", namespace="seaweedfs-test")
    stub_cli(backend, monkeypatch, {
        "get storageclass": SC_LONGHORN,
        "get secret": EXISTING_SECRET,
    })
    info = backend.create(
        cluster(access_key="new", secret_key="rotate-me-1234"), wait_for_ready=False
    )
    assert info.access_key == "new"


def test_new_cluster_still_generates_credentials(backend, stub_helm, monkeypatch):
    stub_helm(backend)
    stub_cli(backend, monkeypatch, {"get storageclass": SC_LONGHORN})
    info = backend.create(cluster(), wait_for_ready=False)
    assert len(info.access_key) == 16


# -- Vault --------------------------------------------------------------
# tests/conftest.py's isolated_vault_client fixture already points
# default_vault_client() at an empty state dir, so create() below is
# exercised against the real (no-op-safe) client in the "absent" test,
# and against a recording stand-in where the exact path/data matters.
def test_create_succeeds_even_though_no_vault_exists(backend, stub_helm, monkeypatch):
    """The real contract: a capability's own deployment must never
    depend on whether a completely separate one (Vault) happens to
    exist. No mocking of the vault client at all here -- this exercises
    multistack/vault/client.py's own catch-everything behaviour end to
    end, against the empty init-file directory tests/conftest.py already
    points it at."""
    stub_helm(backend)
    stub_cli(backend, monkeypatch, {"get storageclass": SC_LONGHORN})

    info = backend.create(cluster(), wait_for_ready=False)

    assert info.access_key and info.secret_key


def test_create_stores_the_generated_credentials_at_the_tenants_own_path(
    backend, stub_helm, monkeypatch,
):
    stub_helm(backend)
    stub_cli(backend, monkeypatch, {"get storageclass": SC_LONGHORN})
    calls = []

    class FakeVaultClient:
        def store_secret(self, path, data, *, deployment_name=None):
            calls.append((path, data, deployment_name))
            return True

    monkeypatch.setattr(
        "multistack.backends.seaweedfs_client.default_vault_client",
        lambda: FakeVaultClient(),
    )

    info = backend.create(cluster(), wait_for_ready=False)

    assert calls == [(
        "objectstore/seaweedfs-test",
        {"access_key": info.access_key, "secret_key": info.secret_key},
        # The same name @track_create("objectstore", ...) records this
        # cluster under -- see cluster() below (name="seaweedfs-test").
        "seaweedfs-test",
    )]


# -- storage inspection ---------------------------------------------------
def test_endpoint_is_the_in_cluster_s3_url(backend):
    """What Inference.s3_endpoint_url is handed."""
    assert backend.endpoint(cluster()) == (
        "http://seaweedfs-test-s3.seaweedfs-test.svc.cluster.local:8333"
    )


def test_pvc_sizes_pairs_every_claim_with_its_request(backend, monkeypatch):
    stub_cli(backend, monkeypatch, {
        "get pvc": "data1-seaweedfs-test-volume-0 30Gi\n"
                   "data1-seaweedfs-test-volume-1 30Gi\n"
                   "data1-seaweedfs-test-volume-2 30Gi\n",
    })
    sizes = backend.pvc_sizes(cluster())
    assert len(sizes) == 3
    assert sizes["data1-seaweedfs-test-volume-1"] == "30Gi"


def test_pvc_sizes_survives_a_claim_with_no_size(backend, monkeypatch):
    """A PVC still being provisioned has no requests.storage yet, and a
    ragged line must not take the whole listing down."""
    stub_cli(backend, monkeypatch, {
        "get pvc": "data1-seaweedfs-test-volume-0 30Gi\ndata1-seaweedfs-test-volume-1\n",
    })
    sizes = backend.pvc_sizes(cluster())
    assert sizes == {"data1-seaweedfs-test-volume-0": "30Gi"}


# -- destructive ----------------------------------------------------------
def test_delete_pvcs_removes_every_claim_and_names_them(backend, monkeypatch):
    """Irreversible: the cluster's data goes with them. It returns the
    names so a caller can report what it destroyed rather than a count."""
    calls = []
    stub_cli(backend, monkeypatch, {
        "get pvc": "data1-seaweedfs-test-volume-0\ndata1-seaweedfs-test-volume-1\n",
    }, record=calls)

    removed = backend.delete_pvcs(cluster())

    assert removed == ["data1-seaweedfs-test-volume-0", "data1-seaweedfs-test-volume-1"]
    deletes = [" ".join(c) for c in calls if isinstance(c, list) and "delete pvc" in " ".join(c)]
    assert len(deletes) == 2
    # --ignore-not-found, so a partial teardown can be finished by re-running.
    assert all("--ignore-not-found" in d for d in deletes)


def test_delete_pvcs_on_a_cluster_with_none_is_not_an_error(backend, monkeypatch):
    stub_cli(backend, monkeypatch, {"get pvc": ""})
    assert backend.delete_pvcs(cluster()) == []


def test_destructive_storage_operations_are_recorded_in_state(backend, monkeypatch):
    """resize_storage and delete_pvcs change a live deployment. Before
    track_update they recorded nothing, so the row kept asserting the
    shape the cluster had when it was created."""
    state = tracking.default_state_manager()
    state.start("seaweedfs-test", component_type="objectstore")
    state.mark_healthy("seaweedfs-test")

    stub_cli(backend, monkeypatch, {
        "get pvc": "data1-seaweedfs-test-volume-0 30Gi\n",
    })
    backend.resize_storage(cluster(volume_size="30Gi"), "40Gi")
    # Still healthy -- an update restores the prior status rather than
    # asserting one of its own.
    assert state.get("seaweedfs-test").status.value == "provisioned"


def test_a_refused_resize_leaves_the_cluster_healthy(backend, monkeypatch):
    """A rejection is the SDK working, not the deployment breaking.

    Both SeaweedFSStorageError paths out of resize_storage are
    pre-flight -- a shrink, which Kubernetes cannot do, and no PVCs to
    resize -- so the cluster is exactly as it was.
    """
    state = tracking.default_state_manager()
    state.start("seaweedfs-test", component_type="objectstore")
    state.mark_healthy("seaweedfs-test")

    stub_cli(backend, monkeypatch, {"get pvc": ""})
    with pytest.raises(SeaweedFSStorageError, match="No PVCs"):
        backend.resize_storage(cluster(), "40Gi")
    assert state.get("seaweedfs-test").status.value == "provisioned"

    stub_cli(backend, monkeypatch, {"get pvc": "data1-seaweedfs-test-volume-0 30Gi\n"})
    with pytest.raises(SeaweedFSStorageError, match="Cannot shrink"):
        backend.resize_storage(cluster(volume_size="30Gi"), "10Gi")
    assert state.get("seaweedfs-test").status.value == "provisioned"


def test_an_update_that_breaks_partway_does_mark_failed(backend, monkeypatch):
    """The other half of the rule: only a refusal is exempt. Something
    that fails mid-flight may have applied half its changes, which is
    exactly the state someone needs to find."""
    state = tracking.default_state_manager()
    state.start("seaweedfs-test", component_type="objectstore")
    state.mark_healthy("seaweedfs-test")

    stub_cli(backend, monkeypatch, {"get pvc": "data1-seaweedfs-test-volume-0 30Gi\n"})
    monkeypatch.setattr(backend, "_kubectl", _explode)

    with pytest.raises(RuntimeError, match="apiserver went away"):
        backend.resize_storage(cluster(volume_size="30Gi"), "40Gi")

    row = state.get("seaweedfs-test")
    assert row.status.value == "failed" and "apiserver went away" in row.error


def _explode(*args, **kwargs):
    raise RuntimeError("apiserver went away")
