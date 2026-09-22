"""Tests for LonghornDriver's logic without a cluster or nodes: the
prerequisite checks (which are the component's real value), helm argument
construction, and StorageClass verification. Node commands and local CLIs
are stubbed."""
import pytest

from multistack import RKE2Node, Storage
from multistack.storage import LonghornOptions
from multistack.storage.drivers.longhorn import (
    LonghornDriver,
    LonghornError,
    LonghornPrerequisiteError,
)

KUBECONFIG = "/tmp/kc.yaml"


@pytest.fixture
def backend():
    return LonghornDriver()


@pytest.fixture
def node():
    return RKE2Node(address="10.0.0.1", user="ubuntu", ssh_key="/keys/id")


def stub_probes(backend, monkeypatch, overrides=None):
    """Makes every node probe report a healthy node, except where
    `overrides` maps a substring of the probed command to a reply."""
    healthy = {
        "iscsiadm": "yes",
        "is-active iscsid": "active",
        "mount.nfs4": "yes",
        "is-active multipathd": "inactive",
        "/var/lib/kubelet": "yes",
        "apt-get": "yes",
    }
    replies = {**healthy, **(overrides or {})}

    def fake_probe(node, command, what):
        for needle, reply in replies.items():
            if needle in command:
                return reply
        return "yes"  # the REQUIRED_BINARIES checks

    monkeypatch.setattr(backend, "_probe", fake_probe)
    monkeypatch.setattr(backend, "_check_passwordless_sudo", lambda node: None)


def spec(**kwargs) -> Storage:
    defaults = dict(type="longhorn", kubeconfig_path=KUBECONFIG)
    return Storage(**{**defaults, **kwargs})


# -- prerequisite checks -------------------------------------------------
def test_healthy_node_produces_no_warnings(backend, node, monkeypatch):
    # replica_count matched to the node count: the default of 3 against a
    # single node correctly warns (see the replica test below).
    stub_probes(backend, monkeypatch)
    assert backend.check_prerequisites(spec(replica_count=1), [node]) == []


def test_missing_open_iscsi_is_a_hard_failure(backend, node, monkeypatch):
    # Longhorn attaches volumes via iscsiadm; without it volumes silently
    # fail to attach much later.
    stub_probes(backend, monkeypatch, {"iscsiadm": "no"})

    with pytest.raises(LonghornPrerequisiteError, match="open-iscsi is not installed"):
        backend.check_prerequisites(spec(), [node])


def test_stopped_iscsid_is_a_hard_failure(backend, node, monkeypatch):
    # This is the state a fresh Ubuntu node is actually in after installing
    # open-iscsi, and the one that produces the most confusing failures.
    stub_probes(backend, monkeypatch, {"is-active iscsid": "inactive"})

    with pytest.raises(LonghornPrerequisiteError, match="not.*running"):
        backend.check_prerequisites(spec(), [node])


def test_missing_nfs_client_is_fatal_only_when_rwx_is_wanted(backend, node, monkeypatch):
    stub_probes(backend, monkeypatch, {"mount.nfs4": "no"})

    with pytest.raises(LonghornPrerequisiteError, match="NFSv4 client"):
        backend.check_prerequisites(
            spec(options=LonghornOptions(enable_rwx=True)), [node]
        )

    warnings = backend.check_prerequisites(
        spec(options=LonghornOptions(enable_rwx=False)), [node]
    )
    assert any("RWX volumes" in w for w in warnings)


def test_missing_required_binary_is_a_hard_failure(backend, node, monkeypatch):
    stub_probes(backend, monkeypatch, {"command -v blkid": "no"})

    with pytest.raises(LonghornPrerequisiteError, match="blkid"):
        backend.check_prerequisites(spec(), [node])


def test_active_multipathd_warns_rather_than_blocking(backend, node, monkeypatch):
    # A real cause of MountVolume.SetUp failures, but fixing it means editing
    # multipath.conf on a live node — too blunt to do implicitly.
    stub_probes(backend, monkeypatch, {"is-active multipathd": "active"})

    warnings = backend.check_prerequisites(spec(), [node])

    assert any("multipathd" in w for w in warnings)


def test_nonstandard_kubelet_dir_warns(backend, node, monkeypatch):
    stub_probes(backend, monkeypatch, {"/var/lib/kubelet": "no"})

    warnings = backend.check_prerequisites(spec(), [node])

    assert any("KUBELET_ROOT_DIR" in w for w in warnings)


def test_replica_count_above_node_count_warns(backend, node, monkeypatch):
    stub_probes(backend, monkeypatch)

    warnings = backend.check_prerequisites(spec(replica_count=3), [node])

    assert any("exceeds the 1 node(s)" in w for w in warnings)


# -- prerequisite installation -------------------------------------------
def test_install_prerequisites_enables_iscsid_on_debian(backend, node, monkeypatch):
    stub_probes(backend, monkeypatch)
    ran = []
    monkeypatch.setattr(backend, "_run", lambda node, cmd, **kw: ran.append(cmd) or "")

    backend.install_prerequisites([node])

    assert any("open-iscsi" in c and "nfs-common" in c for c in ran)
    # Installing the package isn't enough — Ubuntu leaves iscsid inactive.
    assert "systemctl enable --now iscsid" in ran


def test_install_prerequisites_uses_dnf_when_apt_is_absent(backend, node, monkeypatch):
    stub_probes(backend, monkeypatch, {"apt-get": "no", "command -v dnf": "yes"})
    ran = []
    monkeypatch.setattr(backend, "_run", lambda node, cmd, **kw: ran.append(cmd) or "")

    backend.install_prerequisites([node])

    assert any("iscsi-initiator-utils" in c for c in ran)


def test_install_prerequisites_refuses_to_guess_a_package_manager(backend, node, monkeypatch):
    stub_probes(
        backend,
        monkeypatch,
        {"apt-get": "no", "command -v dnf": "no", "command -v yum": "no"},
    )
    monkeypatch.setattr(backend, "_run", lambda node, cmd, **kw: "")

    with pytest.raises(LonghornPrerequisiteError, match="no apt-get, dnf or yum"):
        backend.install_prerequisites([node])


# -- install -------------------------------------------------------------
def test_uncovered_cluster_nodes_are_flagged(backend, node, monkeypatch):
    # Longhorn's node components are a DaemonSet, so a node left out of the
    # prerequisite check still runs longhorn-manager — and crash-loops it if
    # open-iscsi is missing. Checking a subset must not look like success.
    monkeypatch.setattr(
        backend,
        "_kubectl",
        lambda storage, *args: "rke2-cp01=10.0.0.1\nmcw-all-series=192.0.2.13\n",
    )

    warnings = backend._check_cluster_coverage(
        spec(),
        [RKE2Node(address="10.0.0.1")],
    )

    assert any("mcw-all-series (192.0.2.13)" in w for w in warnings)
    assert any("DaemonSet" in w for w in warnings)


def test_full_cluster_coverage_produces_no_warning(backend, monkeypatch):
    monkeypatch.setattr(
        backend, "_kubectl", lambda storage, *args: "a=10.0.0.1\nb=10.0.0.2\n"
    )

    assert backend._check_cluster_coverage(
        spec(),
        [RKE2Node(address="10.0.0.1"), RKE2Node(address="10.0.0.2")],
    ) == []



# -- helm seam ------------------------------------------------------------
#
# The driver reaches Helm through `_helm_runner()`, not `_local()`. A test
# that stubs only `_local` no longer intercepts the install: the call goes
# out through HelmRunner to the real helm binary, which then tries to reach
# a real cluster and fetch a real chart over the network. That is how these
# tests failed after the pyhelm port -- not with an assertion error but by
# hanging on charts.longhorn.io. Stub this seam instead.
#
# Helm and kubectl calls are recorded into one `calls` list so that tests
# asserting an ordering *between* the two still can.


class _FakeRunner:
    def __init__(self, calls):
        self.calls = calls

    def install_or_upgrade(self, release, **kwargs):
        self.calls.append(("install", release, kwargs))
        return {"release": release}

    def uninstall(self, release, **kwargs):
        self.calls.append(("uninstall", release, kwargs))


def _stub(backend, monkeypatch, kubectl=lambda argv: "longhorn\n"):
    """Stubs every outward call and returns the shared recording list."""
    calls = []
    monkeypatch.setattr(backend, "_require_cli", lambda name: None)
    monkeypatch.setattr(
        backend, "_local",
        lambda argv: calls.append(("local", argv)) or kubectl(argv),
    )
    monkeypatch.setattr(
        backend, "_helm_runner",
        lambda storage: calls.append(("runner", storage.kubeconfig_path))
        or _FakeRunner(calls),
    )
    return calls


def _install(calls) -> dict:
    """The kwargs of the one install_or_upgrade call."""
    return next(c[2] for c in calls if c[0] == "install")


def _argvs(calls) -> list:
    return [c[1] for c in calls if c[0] == "local"]


def test_create_scopes_every_cluster_command_to_the_given_kubeconfig(backend, monkeypatch):
    """Neither helm nor kubectl may ever resolve a cluster ambiently.

    Helm is scoped by the kubeconfig the runner is built with rather than
    by a --kubeconfig argv entry, so this asserts on the runner's
    construction; kubectl is still argv and is checked as before.
    """
    calls = _stub(backend, monkeypatch)

    assert backend.create(spec(replica_count=2)) == "longhorn"

    scoped_to = [c[1] for c in calls if c[0] == "runner"]
    assert scoped_to == [KUBECONFIG], "helm runner not scoped to the spec's kubeconfig"

    kubectl = next(a for a in _argvs(calls) if a[0] == "kubectl")
    assert "--kubeconfig" in kubectl
    assert kubectl[kubectl.index("--kubeconfig") + 1] == KUBECONFIG


def test_create_passes_spec_values_and_waits(backend, monkeypatch):
    calls = _stub(backend, monkeypatch)

    backend.create(spec(replica_count=2))

    kw = _install(calls)
    # install_or_upgrade is idempotent by construction -- it is the
    # `helm upgrade --install` of the Helm layer, so there is no flag to
    # assert the way the --set era had to check for "--install".
    assert kw["wait"] is True
    assert kw["atomic"] is True
    assert kw["values"]["defaultSettings"]["defaultReplicaCount"] == 2


def test_create_passes_booleans_as_real_booleans(backend, monkeypatch):
    """Under `--set` a False had to be rendered as the string "false",
    because Python's "False" is truthy to helm. Values now travel as JSON
    on stdin, so a real bool is correct and the rendered string would be
    the bug -- helm would receive the *string* "false", which is truthy.
    """
    calls = _stub(backend, monkeypatch)

    backend.create(spec(default_storage_class=False))

    assert _install(calls)["values"]["persistence"]["defaultClass"] is False


def test_create_pins_chart_version_when_given(backend, monkeypatch):
    calls = _stub(backend, monkeypatch)

    backend.create(spec(chart_version="1.9.1"))

    assert _install(calls)["chart_version"] == "1.9.1"


def test_create_fails_loudly_if_no_storage_class_appears(backend, monkeypatch):
    # A "Deployed" release with no StorageClass leaves every later PVC
    # Pending, and the blame lands on whatever tried to use the storage.
    _stub(backend, monkeypatch, kubectl=lambda argv: "")

    with pytest.raises(LonghornError, match="no 'longhorn' StorageClass"):
        backend.create(spec())


def test_create_validates_the_spec_before_touching_anything(backend, monkeypatch):
    monkeypatch.setattr(
        backend, "_require_cli", lambda name: pytest.fail("should not reach the CLI")
    )
    monkeypatch.setattr(
        backend, "_helm_runner", lambda storage: pytest.fail("should not reach helm")
    )

    with pytest.raises(ValueError, match="kubeconfig_path is required"):
        backend.create(spec(kubeconfig_path=""))


def test_delete_leaves_volume_data_alone(backend, monkeypatch):
    calls = _stub(backend, monkeypatch, kubectl=lambda argv: "")

    backend.delete(spec())

    assert any(c[0] == "uninstall" for c in calls)
    # Deleting CRDs would destroy every volume in the cluster.
    assert not any("crd" in " ".join(a).lower() for a in _argvs(calls))


def test_delete_sets_the_deleting_confirmation_flag_first(backend, monkeypatch):
    """Longhorn's uninstaller job refuses to run unless this setting is
    true. Without it `helm uninstall` removes the release while the
    uninstaller never completes, leaving the namespace Terminating behind
    CRDs and finalizers. Taken from a teammate's longhorn_k8s_sdk, which
    had it and ours didn't.

    The ordering now spans two seams -- kubectl sets the flag, HelmRunner
    does the uninstall -- which is why both record into one list.
    """
    calls = _stub(backend, monkeypatch, kubectl=lambda argv: "")

    backend.delete(spec())

    flag = next(
        (c for c in calls
         if c[0] == "local" and "settings.longhorn.io" in " ".join(c[1])),
        None,
    )
    uninstall = next((c for c in calls if c[0] == "uninstall"), None)
    assert flag is not None, "deleting-confirmation-flag was never set"
    assert "deleting-confirmation-flag" in " ".join(flag[1])
    assert '{"value":"true"}' in " ".join(flag[1])
    assert calls.index(flag) < calls.index(uninstall), "flag must precede uninstall"


def test_delete_proceeds_when_the_flag_cannot_be_set(backend, monkeypatch):
    """Best-effort on purpose: an older Longhorn, or a release that never
    finished installing, has no such setting -- and that is not a reason to
    refuse to uninstall."""
    def flaky(argv):
        if "settings.longhorn.io" in " ".join(argv):
            raise LonghornError("settings.longhorn.io not found")
        return ""

    calls = _stub(backend, monkeypatch, kubectl=flaky)

    backend.delete(spec())          # must not raise
    assert any(c[0] == "uninstall" for c in calls)
