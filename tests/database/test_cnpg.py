"""Tests for the database capability's cnpg driver: the two lifecycles,
and what it refuses to destroy without being asked.

The capability arrived with none, so these start from what the public
surface promises rather than from what the code happens to do -- which
is how the wait_for_absent signature bug and the missing scheduling
both surfaced.

Driven through `DatabaseBackend` rather than the driver directly,
because the state-tracking section below is testing the decorators the
backend carries -- the driver itself does no recording.

Two seams: `stub_helm` for the operator's Helm release (the shared
recording runner), and `_local` for kubectl, whose argv is the only
record of what would have been applied or destroyed.
"""
import json

import pytest

import multistack.state.tracking as tracking
from multistack.database import (
    CNPGOptions,
    Database,
    DatabaseBackend,
    DatabaseClusterNotFoundError,
    DatabaseConfig,
    DatabaseError,
    DatabasePrerequisiteError,
    DatabaseTimeoutError,
)
from multistack.database.drivers.cnpg import CNPGDriver

KUBECONFIG = "/tmp/kc.yaml"
CRD = "clusters.postgresql.cnpg.io"
CLUSTER_RESOURCE = "clusters.postgresql.cnpg.io"


def database(**kwargs) -> DatabaseConfig:
    base = dict(name="admin_control_plane", owner="admin", password="s3cret")
    base.update(kwargs)
    return DatabaseConfig(**base)


def cnpg(**kwargs) -> Database:
    """A Database, with the operator settings routed into the
    implementation's options so every call site below reads as it
    always did."""
    opt_keys = {"operator_release_name", "operator_namespace",
                "operator_chart", "operator_chart_version", "values",
                "install_timeout", "crd_name"}
    opts = {k: kwargs.pop(k) for k in list(kwargs) if k in opt_keys}
    base = dict(kubeconfig_path=KUBECONFIG, name="admin-pg",
                namespace="platform-db", instances=1, database=database())
    base.update(kwargs)
    if opts:
        base["options"] = CNPGOptions(**opts)
    return Database(**base)


@pytest.fixture
def backend(monkeypatch):
    # Both lifecycles declare REQUIRES = ("cluster",), and @track_create
    # refuses to run until the state layer holds a healthy row for it.
    state = tracking.default_state_manager()
    state.start("test-cluster", component_type="cluster")
    state.mark_healthy("test-cluster")

    b = DatabaseBackend()
    # The CLI probe and the cluster check both talk to the outside world;
    # neither is what these tests are about. Patched on the driver class
    # rather than an instance, because CapabilityBackend constructs and
    # caches its own.
    monkeypatch.setattr(CNPGDriver, "_require_cli", lambda self, name: None)
    monkeypatch.setattr(
        "multistack.database.drivers.cnpg.require_cluster",
        lambda path, capability="?", **k: "stubbed",
    )
    # And again at the capability layer: migrating moved the generic
    # REQUIRES check into CapabilityBackend.verify_requirements(), which
    # runs before dispatch, so the driver's own stub is no longer the
    # only cluster round-trip in the path.
    monkeypatch.setattr(
        "multistack.capability.require_cluster",
        lambda path, capability="?", **k: "stubbed",
    )
    return b


@pytest.fixture
def kubectl(backend, monkeypatch):
    """Records kubectl argv, and answers `get` from a fake cluster."""

    class Fake:
        def __init__(self):
            self.runs = []
            self.present = {CRD}          # the CRD exists by default
            self.phase = "Cluster in healthy state"

        def __call__(self, argv, check=True):
            self.runs.append(argv)
            args = argv[3:]               # past kubectl --kubeconfig <path>

            if args[0] == "get":
                return self._get(args)
            return ""

        def _get(self, args):
            kind, name = args[1], args[2]
            key = f"{kind}/{name}"
            if key not in self.present:
                return ""
            if "json" in args:
                return json.dumps({"status": {"phase": self.phase}})
            return key

        # -- shaping the fake cluster ---------------------------------
        def given(self, *keys):
            self.present.update(keys)

        def given_no_crd(self):
            self.present.discard(CRD)

        def ran(self, *fragment):
            return [r for r in self.runs
                    if all(f in r for f in fragment)]

    fake = Fake()
    fake.present = {CRD}
    # `get crd <name>` arrives as args = ["get", "crd", <name>, ...],
    # so the CRD is keyed by its own name.
    monkeypatch.setattr(CNPGDriver, "_local",
                        lambda self, argv, check=True: fake(argv, check))
    monkeypatch.setattr(
        "multistack.database.drivers.cnpg.apply",
        lambda path, manifest, **k: fake.runs.append(["apply", manifest]) or "",
    )
    return fake


def applied(kubectl, kind):
    """The last manifest of `kind` handed to kubectl apply."""
    for entry in reversed(kubectl.runs):
        if entry[0] == "apply" and entry[1].get("kind") == kind:
            return entry[1]
    raise AssertionError(f"no {kind} was applied")


def _crd_key(fake):
    return CRD


# -- operator lifecycle ---------------------------------------------------
def test_create_installs_the_operator_chart(backend, stub_helm, kubectl):
    kubectl.given(f"crd/{CRD}")
    runner = stub_helm(CNPGDriver)
    spec = cnpg()

    backend.create(spec)

    installed = runner.installed(spec.operator_release_name)
    assert len(installed) == 1
    assert installed[0]["namespace"] == "cnpg-system"
    assert installed[0]["chart"] == "cloudnative-pg"
    assert installed[0]["chart_version"] == "0.29.0", "the version is pinned"


def test_create_does_not_reinstall_an_operator_that_is_there(
    backend, stub_helm, kubectl,
):
    """Unlike create_cluster, this is a no-op rather than an upgrade --
    update() is the method that reconciles."""
    kubectl.given(f"crd/{CRD}")
    runner = stub_helm(CNPGDriver)
    spec = cnpg()
    runner.given_deployed(spec.operator_release_name,
                          namespace=spec.operator_namespace)

    backend.create(spec)

    assert runner.installs == []


def test_create_refuses_when_the_crd_did_not_appear(
    backend, stub_helm, kubectl,
):
    """A chart that installed without registering its CRD leaves every
    later Cluster apply failing with an unhelpful "no matches for kind"."""
    stub_helm(CNPGDriver)
    kubectl.given_no_crd()

    with pytest.raises(DatabasePrerequisiteError, match="was not found"):
        backend.create(cnpg())


def test_update_reconciles_the_operator(backend, stub_helm, kubectl):
    kubectl.given(f"crd/{CRD}")
    runner = stub_helm(CNPGDriver)
    spec = cnpg(values={"replicaCount": 2})

    backend.update(spec)

    assert runner.installed(spec.operator_release_name)[-1]["values"] == {
        "replicaCount": 2}


def test_delete_removes_the_release_and_its_namespace(
    backend, stub_helm, kubectl,
):
    kubectl.given(f"crd/{CRD}", "namespace/cnpg-system")
    runner = stub_helm(CNPGDriver)
    spec = cnpg()
    runner.given_deployed(spec.operator_release_name,
                          namespace=spec.operator_namespace)

    backend.delete(spec)

    assert runner.uninstalls[-1]["release"] == "cnpg"
    assert runner.uninstalls[-1]["missing_ok"] is True
    assert kubectl.ran("delete", "namespace", "cnpg-system")


def test_delete_can_keep_the_namespace(backend, stub_helm, kubectl):
    kubectl.given(f"crd/{CRD}", "namespace/cnpg-system")
    runner = stub_helm(CNPGDriver)
    spec = cnpg()
    runner.given_deployed(spec.operator_release_name,
                          namespace=spec.operator_namespace)

    backend.delete(spec, remove_namespace=False)

    assert not kubectl.ran("delete", "namespace")


def test_the_crds_survive_deleting_the_operator(
    backend, stub_helm, kubectl,
):
    """Cluster-scoped and shared: dropping them would take every
    PostgreSQL Cluster on the cluster with them, in any namespace."""
    kubectl.given(f"crd/{CRD}", "namespace/cnpg-system")
    runner = stub_helm(CNPGDriver)
    runner.given_deployed("cnpg", namespace="cnpg-system")

    backend.delete(cnpg())

    assert not kubectl.ran("delete", "crd")


# -- cluster lifecycle ----------------------------------------------------
def test_create_cluster_applies_a_secret_then_the_cluster(
    backend, stub_helm, kubectl,
):
    stub_helm(CNPGDriver)
    kubectl.given(f"crd/{CRD}", "namespace/platform-db")
    spec = cnpg()

    backend.create_cluster(spec, wait_for_ready=False)

    kinds = [e[1]["kind"] for e in kubectl.runs if e[0] == "apply"]
    assert kinds == ["Secret", "Cluster"], (
        "the bootstrap Secret has to exist before the Cluster that names it"
    )


def test_the_bootstrap_secret_carries_the_owner_and_password(
    backend, stub_helm, kubectl,
):
    stub_helm(CNPGDriver)
    kubectl.given(f"crd/{CRD}", "namespace/platform-db")

    backend.create_cluster(cnpg(), wait_for_ready=False)

    secret = applied(kubectl, "Secret")
    assert secret["type"] == "kubernetes.io/basic-auth"
    assert secret["stringData"] == {"username": "admin", "password": "s3cret"}
    assert secret["metadata"]["name"] == "admin-pg-app-secret"


def test_the_cluster_manifest_keeps_postgres_off_the_control_plane(
    backend, stub_helm, kubectl,
):
    """The manifest is the only place this can be said: CloudNativePG
    emits no scheduling constraints of its own and rke2-cp01 carries no
    taint, so a Cluster applied without this is eligible for the node
    running etcd."""
    stub_helm(CNPGDriver)
    kubectl.given(f"crd/{CRD}", "namespace/platform-db")

    backend.create_cluster(cnpg(), wait_for_ready=False)

    affinity = applied(kubectl, "Cluster")["spec"]["affinity"]
    terms = affinity["nodeAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
    expressions = [e for t in terms for e in t["matchExpressions"]]
    assert {"key": "node-role.kubernetes.io/control-plane",
            "operator": "DoesNotExist"} in expressions


def test_a_node_selector_and_tolerations_reach_the_manifest(
    backend, stub_helm, kubectl,
):
    """Inside spec.affinity, which is where CloudNativePG's schema puts
    them -- not at the top level like a Deployment."""
    stub_helm(CNPGDriver)
    kubectl.given(f"crd/{CRD}", "namespace/platform-db")

    backend.create_cluster(
        cnpg(node_selector={"kubernetes.io/hostname": "rke2-wrk-2"},
             tolerations=[{"key": "dedicated", "operator": "Exists"}]),
        wait_for_ready=False,
    )

    affinity = applied(kubectl, "Cluster")["spec"]["affinity"]
    assert affinity["nodeSelector"] == {"kubernetes.io/hostname": "rke2-wrk-2"}
    assert affinity["tolerations"] == [{"key": "dedicated",
                                        "operator": "Exists"}]


def test_the_storage_class_is_only_set_when_given(
    backend, stub_helm, kubectl,
):
    """An empty storageClass is not the same as an absent one: it binds
    to no class at all rather than to the cluster default."""
    stub_helm(CNPGDriver)
    kubectl.given(f"crd/{CRD}", "namespace/platform-db")

    backend.create_cluster(cnpg(), wait_for_ready=False)
    assert "storageClass" not in applied(kubectl, "Cluster")["spec"]["storage"]

    backend.create_cluster(cnpg(name="other", storage_class="longhorn"),
                           wait_for_ready=False)
    assert applied(kubectl, "Cluster")["spec"]["storage"]["storageClass"] == (
        "longhorn")


def test_create_cluster_creates_the_namespace_only_if_absent(
    backend, stub_helm, kubectl,
):
    stub_helm(CNPGDriver)
    kubectl.given(f"crd/{CRD}")          # no namespace

    backend.create_cluster(cnpg(), wait_for_ready=False)
    assert kubectl.ran("create", "namespace", "platform-db")

    kubectl.runs.clear()
    kubectl.given("namespace/platform-db")
    backend.create_cluster(cnpg(name="second"), wait_for_ready=False)
    assert not kubectl.ran("create", "namespace")


def test_create_cluster_is_a_no_op_when_one_exists(
    backend, stub_helm, kubectl,
):
    """Re-running a provisioning script must not reapply a Secret over a
    live cluster -- CNPG reads bootstrap.initdb.secret only at bootstrap,
    so a rewritten password would take effect nowhere and diverge from
    the role that actually exists."""
    stub_helm(CNPGDriver)
    kubectl.given(f"crd/{CRD}", "namespace/platform-db",
                  f"{CLUSTER_RESOURCE}/admin-pg")

    backend.create_cluster(cnpg(), wait_for_ready=False)

    assert not [e for e in kubectl.runs if e[0] == "apply"]


def test_update_cluster_of_a_missing_cluster_is_refused(
    backend, stub_helm, kubectl,
):
    """Applying would create it -- turning a typo'd name into a second
    database nobody asked for, with its own PVCs."""
    stub_helm(CNPGDriver)
    kubectl.given(f"crd/{CRD}")

    with pytest.raises(DatabaseClusterNotFoundError, match="was not found"):
        backend.update_cluster(cnpg(), wait_for_ready=False)


def test_update_cluster_reapplies_the_cluster_only(
    backend, stub_helm, kubectl,
):
    """Not the Secret: CNPG reads it at bootstrap and never again, so
    rewriting it on every update only invites drift."""
    stub_helm(CNPGDriver)
    kubectl.given(f"crd/{CRD}", f"{CLUSTER_RESOURCE}/admin-pg")

    backend.update_cluster(cnpg(instances=3), wait_for_ready=False)

    kinds = [e[1]["kind"] for e in kubectl.runs if e[0] == "apply"]
    assert kinds == ["Cluster"]
    assert applied(kubectl, "Cluster")["spec"]["instances"] == 3


# -- deletion, which is the irreversible half -----------------------------
def test_delete_cluster_waits_for_the_resource_to_go(
    backend, stub_helm, kubectl, monkeypatch,
):
    """The regression that made this path dead code. wait_for_absent
    takes the object to watch -- (kubeconfig_path, kind, name,
    namespace) -- not a predicate. Called with a lambda it raised
    TypeError for three missing positional arguments, so
    delete_cluster(), whose `wait` defaults to True, could never
    complete.
    """
    watched = {}

    def fake_absent(kubeconfig, kind, name, namespace, **kwargs):
        watched.update(kubeconfig=kubeconfig, kind=kind, name=name,
                       namespace=namespace, **kwargs)

    monkeypatch.setattr(
        "multistack.database.drivers.cnpg.wait_for_absent", fake_absent)
    stub_helm(CNPGDriver)
    kubectl.given(f"crd/{CRD}", f"{CLUSTER_RESOURCE}/admin-pg")

    backend.delete_cluster(cnpg())

    assert watched["kubeconfig"] == KUBECONFIG
    assert watched["kind"] == CLUSTER_RESOURCE
    assert watched["name"] == "admin-pg"
    assert watched["namespace"] == "platform-db"


def test_a_delete_that_never_finishes_says_so(
    backend, stub_helm, kubectl, monkeypatch,
):
    """A CNPG Cluster has finalizers, so "the delete returned" and "the
    cluster is gone" are different moments."""
    monkeypatch.setattr(
        "multistack.database.drivers.cnpg.wait_for_absent",
        lambda *a, **k: (_ for _ in ()).throw(TimeoutError("still there")))
    stub_helm(CNPGDriver)
    kubectl.given(f"crd/{CRD}", f"{CLUSTER_RESOURCE}/admin-pg")

    with pytest.raises(DatabaseTimeoutError, match="to be deleted"):
        backend.delete_cluster(cnpg())


def test_delete_cluster_does_not_need_the_password(
    backend, stub_helm, kubectl, monkeypatch,
):
    """Removing a database should not require typing its credentials.
    Both read paths and the delete path validate identity only."""
    monkeypatch.setattr(
        "multistack.database.drivers.cnpg.wait_for_absent",
        lambda *a, **k: None)
    stub_helm(CNPGDriver)
    kubectl.given(f"crd/{CRD}", f"{CLUSTER_RESOURCE}/admin-pg")

    no_password = Database(kubeconfig_path=KUBECONFIG, name="admin-pg",
                           namespace="platform-db")

    backend.delete_cluster(no_password, remove_secret=True)

    assert kubectl.ran("delete", CLUSTER_RESOURCE, "admin-pg")
    assert kubectl.ran("delete", "secret", "admin-pg-app-secret")


def test_delete_cluster_can_keep_the_secret(
    backend, stub_helm, kubectl, monkeypatch,
):
    monkeypatch.setattr(
        "multistack.database.drivers.cnpg.wait_for_absent",
        lambda *a, **k: None)
    stub_helm(CNPGDriver)
    kubectl.given(f"crd/{CRD}", f"{CLUSTER_RESOURCE}/admin-pg")

    backend.delete_cluster(cnpg(), remove_secret=False)

    assert not kubectl.ran("delete", "secret")


def test_deleting_an_absent_cluster_is_not_an_error(
    backend, stub_helm, kubectl,
):
    stub_helm(CNPGDriver)
    kubectl.given(f"crd/{CRD}")

    backend.delete_cluster(cnpg())

    assert not kubectl.ran("delete", CLUSTER_RESOURCE)


def test_deleting_a_cluster_leaves_the_operator_alone(
    backend, stub_helm, kubectl, monkeypatch,
):
    monkeypatch.setattr(
        "multistack.database.drivers.cnpg.wait_for_absent",
        lambda *a, **k: None)
    runner = stub_helm(CNPGDriver)
    kubectl.given(f"crd/{CRD}", f"{CLUSTER_RESOURCE}/admin-pg")

    backend.delete_cluster(cnpg())

    assert runner.uninstalls == []
    assert not kubectl.ran("delete", "namespace")


# -- status and readiness -------------------------------------------------
def test_cluster_exists_reflects_the_cluster(backend, stub_helm, kubectl):
    stub_helm(CNPGDriver)
    assert backend.cluster_exists(cnpg()) is False
    kubectl.given(f"{CLUSTER_RESOURCE}/admin-pg")
    assert backend.cluster_exists(cnpg()) is True


def test_cluster_status_raises_for_an_absent_cluster(
    backend, stub_helm, kubectl,
):
    stub_helm(CNPGDriver)
    with pytest.raises(DatabaseClusterNotFoundError, match="was not found"):
        backend.cluster_status(cnpg())


def test_unparseable_status_is_not_reported_as_absent(
    backend, stub_helm, kubectl, monkeypatch,
):
    """"kubectl returned something I cannot read" and "there is no such
    cluster" are different answers, and only one of them means it is
    safe to create."""
    stub_helm(CNPGDriver)
    monkeypatch.setattr(CNPGDriver, "_local",
                        lambda self, argv, check=True: "not json")

    with pytest.raises(DatabaseError, match="Failed to parse status"):
        backend.cluster_status(cnpg())


def test_readiness_waits_for_the_healthy_phase(
    backend, stub_helm, kubectl, monkeypatch,
):
    stub_helm(CNPGDriver)
    kubectl.given(f"{CLUSTER_RESOURCE}/admin-pg")
    kubectl.phase = "Setting up primary"

    monkeypatch.setattr(
        "multistack.database.drivers.cnpg.wait_for",
        lambda check, **kwargs: (_ for _ in ()).throw(TimeoutError())
        if not check() else None)

    with pytest.raises(DatabaseTimeoutError, match="to become ready"):
        backend.wait_for_cluster_ready(cnpg())

    kubectl.phase = "Cluster in healthy state"
    backend.wait_for_cluster_ready(cnpg())


# -- state tracking -------------------------------------------------------
def test_the_operator_and_the_database_are_tracked_apart(
    backend, stub_helm, kubectl,
):
    """Two public lifecycles, two rows. "database" is what a dependent's
    REQUIRES asks for and only the Cluster provides it; the operator is
    installable with nothing on top of it."""
    from multistack.state.tracking import default_state_manager

    state = default_state_manager()
    kubectl.given(f"crd/{CRD}", "namespace/platform-db")
    stub_helm(CNPGDriver)
    spec = cnpg()

    backend.create(spec)
    backend.create_cluster(spec, wait_for_ready=False)

    assert state.get("cnpg").component_type == "database-operator"
    assert state.get("admin-pg").component_type == "database"
    assert state.get("admin-pg").status.value == "provisioned"


def test_a_failed_cluster_create_leaves_a_failed_row(
    backend, stub_helm, kubectl, monkeypatch,
):
    from multistack.state.tracking import default_state_manager

    state = default_state_manager()
    stub_helm(CNPGDriver)
    kubectl.given(f"crd/{CRD}", "namespace/platform-db")
    monkeypatch.setattr(
        "multistack.database.drivers.cnpg.apply",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("admission denied")))

    with pytest.raises(RuntimeError, match="admission denied"):
        backend.create_cluster(cnpg(), wait_for_ready=False)

    row = state.get("admin-pg")
    assert row.status.value == "failed" and "admission denied" in row.error


def test_a_refused_update_leaves_the_database_healthy(
    backend, stub_helm, kubectl,
):
    """An update against a cluster that is not there is a rejected
    request, not a broken database -- nothing was touched, so the row
    keeps the status it had."""
    from multistack.state.tracking import default_state_manager

    state = default_state_manager()
    stub_helm(CNPGDriver)
    kubectl.given(f"crd/{CRD}", "namespace/platform-db",
                  f"{CLUSTER_RESOURCE}/admin-pg")
    spec = cnpg()
    backend.create_cluster(spec, wait_for_ready=False)

    kubectl.present.discard(f"{CLUSTER_RESOURCE}/admin-pg")
    with pytest.raises(DatabaseClusterNotFoundError):
        backend.update_cluster(spec, wait_for_ready=False)

    assert state.get("admin-pg").status.value == "provisioned"


def test_deleting_the_cluster_removes_only_its_row(
    backend, stub_helm, kubectl, monkeypatch,
):
    from multistack.state.tracking import default_state_manager

    state = default_state_manager()
    monkeypatch.setattr(
        "multistack.database.drivers.cnpg.wait_for_absent",
        lambda *a, **k: None)
    runner = stub_helm(CNPGDriver)
    kubectl.given(f"crd/{CRD}", "namespace/platform-db")
    spec = cnpg()

    backend.create(spec)
    backend.create_cluster(spec, wait_for_ready=False)
    kubectl.given(f"{CLUSTER_RESOURCE}/admin-pg")

    backend.delete_cluster(spec)

    assert state.get("admin-pg") is None
    assert state.get("cnpg") is not None, (
        "deleting a database must not deregister the operator"
    )


# -- reporting ------------------------------------------------------------
def test_the_backend_says_what_it_is_doing(
    backend, stub_helm, kubectl, capsys,
):
    """It reported through `logging`, and nothing in the SDK configures
    a handler, so a live operator install printed nothing at all."""
    stub_helm(CNPGDriver)
    kubectl.given(f"crd/{CRD}", "namespace/platform-db")
    spec = cnpg()

    backend.create(spec)
    backend.create_cluster(spec, wait_for_ready=False)

    out = capsys.readouterr().out
    assert "[cnpg] installed operator cnpg in cnpg-system" in out
    assert f"[cnpg] cluster admin-pg ready in platform-db at {spec.endpoint}" in out
