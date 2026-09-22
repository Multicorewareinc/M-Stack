"""Tests for the cache capability's valkey driver: what it installs, and
what it refuses to destroy without being asked.

Driven through `CacheBackend` rather than the driver directly, because
the state-tracking section below is testing the decorators the backend
carries -- the driver itself does no recording.

The first backend written against `stub_helm` rather than the old
`_local` subprocess stub — Valkey is pyhelm-native, so there was never a
`helm` argv to intercept. That is the point of the seam: a backend that
talks to `HelmRunner` is testable without a cluster, and without each
test file inventing its own fake.
"""
import pytest

import multistack.state.tracking as tracking
from multistack import Cache
from multistack.cache import CacheBackend, CacheError, ValkeyOptions
from multistack.cache.drivers.valkey import ValkeyDriver

KUBECONFIG = "/tmp/kc.yaml"


@pytest.fixture
def backend(monkeypatch):
    # Cache declares REQUIRES = ("cluster",), and @track_create refuses to
    # run until the state layer holds a healthy row for it. Seeded here
    # rather than per-test, the same way the MinIO fixture does: a cache
    # only ever exists on a cluster that already came up.
    state = tracking.default_state_manager()
    state.start("test-cluster", component_type="cluster")
    state.mark_healthy("test-cluster")

    b = CacheBackend()
    # The CLI probe and the cluster check both talk to the outside world;
    # neither is what these tests are about. Patched on the driver class
    # rather than an instance, because CapabilityBackend constructs and
    # caches its own. require_cluster is a real call — the spec declares
    # a cluster dependency — and tests/test_capability.py covers it.
    monkeypatch.setattr(ValkeyDriver, "_require_cli", lambda self, name: None)
    monkeypatch.setattr(
        "multistack.cache.drivers.valkey.require_cluster",
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


def valkey(**kwargs) -> Cache:
    """A Cache, with chart/values routed into the implementation's options
    so every call site below reads as it always did."""
    opt_keys = {"chart", "chart_version", "values"}
    opts = {k: kwargs.pop(k) for k in list(kwargs) if k in opt_keys}
    base = dict(kubeconfig_path=KUBECONFIG, name="valkey-test",
                namespace="valkey-test")
    base.update(kwargs)
    if opts:
        base["options"] = ValkeyOptions(**opts)
    return Cache(**base)


# -- install --------------------------------------------------------------
def test_create_installs_the_chart_into_the_named_namespace(backend, stub_helm):
    runner = stub_helm(ValkeyDriver)
    spec = valkey()

    release = backend.create(spec)

    installed = runner.installed(spec.name)
    assert len(installed) == 1
    assert installed[0]["namespace"] == "valkey-test"
    assert installed[0]["chart"] == spec.chart
    assert release.name == spec.name


def test_create_is_idempotent_and_upgrades_in_place(backend, stub_helm):
    """Re-running a provisioning script must not fail because it already
    ran — the whole reason the layer exposes install_or_upgrade rather
    than install and upgrade separately."""
    runner = stub_helm(ValkeyDriver)
    spec = valkey()

    backend.create(spec)
    backend.create(spec)

    assert len(runner.installed(spec.name)) == 2
    assert runner.uninstalls == []


def test_spec_values_reach_the_chart(backend, stub_helm):
    runner = stub_helm(ValkeyDriver)
    spec = valkey(values={"architecture": "standalone"})

    backend.create(spec)

    assert runner.installed(spec.name)[0]["values"]["architecture"] == "standalone"


def test_create_raises_when_the_release_is_absent_afterwards(backend, stub_helm):
    """`_find_release` returning None after a deploy means the install
    silently did nothing, which is worse to discover later."""
    runner = stub_helm(ValkeyDriver)
    # Deploy, then drop the release, simulating an install that did not take.
    original = runner.install_or_upgrade

    def install_then_forget(*args, **kwargs):
        result = original(*args, **kwargs)
        runner.releases.clear()
        return result

    runner.install_or_upgrade = install_then_forget
    with pytest.raises(CacheError, match="could not be found"):
        backend.create(valkey())


# -- delete ---------------------------------------------------------------
def test_delete_uninstalls_the_release(backend, stub_helm):
    runner = stub_helm(ValkeyDriver)
    runner.given_deployed("valkey-test", namespace="valkey-test")

    backend.delete(valkey())

    assert runner.uninstalls[-1]["release"] == "valkey-test"
    # missing_ok, so a re-run after a partial teardown is not an error.
    assert runner.uninstalls[-1]["missing_ok"] is True


def test_delete_of_an_absent_release_is_not_an_error(backend, stub_helm):
    runner = stub_helm(ValkeyDriver)          # nothing deployed

    backend.delete(valkey())

    assert runner.uninstalls == []


def test_exists_reflects_the_runner(backend, stub_helm):
    runner = stub_helm(ValkeyDriver)
    assert backend.exists(valkey()) is False
    runner.given_deployed("valkey-test", namespace="valkey-test")
    assert backend.exists(valkey()) is True


# -- state tracking -------------------------------------------------------
def test_create_and_delete_are_recorded_in_state(backend, stub_helm):
    """Valkey was the one deployed capability the state layer could not
    see: a full create/delete cycle against the live cluster left no row,
    so `require_healthy_dependencies` was blind to a Valkey that was
    plainly running."""
    from multistack.state.tracking import default_state_manager

    state = default_state_manager()
    stub_helm(ValkeyDriver)
    spec = valkey()

    backend.create(spec)
    row = state.get(spec.name)
    assert row is not None and row.component_type == "cache"
    assert row.status.value == "provisioned"

    backend.delete(spec)
    assert state.get(spec.name) is None


def test_a_failed_create_leaves_a_failed_row(backend, stub_helm, monkeypatch):
    from multistack.state.tracking import default_state_manager

    state = default_state_manager()
    runner = stub_helm(ValkeyDriver)
    monkeypatch.setattr(runner, "install_or_upgrade", _boom)
    spec = valkey()

    with pytest.raises(RuntimeError, match="helm said no"):
        backend.create(spec)

    row = state.get(spec.name)
    assert row.status.value == "failed" and "helm said no" in row.error


def _boom(*args, **kwargs):
    raise RuntimeError("helm said no")


# -- update ---------------------------------------------------------------
def test_update_deep_merges_values_by_default(backend, stub_helm):
    """A partial update must not silently drop the settings it does not
    mention -- `{"primary": {"resources": ...}}` should not erase
    `primary.persistence`."""
    runner = stub_helm(ValkeyDriver)
    spec = valkey(values={
        "architecture": "standalone",
        "primary": {"persistence": {"enabled": False}},
    })
    backend.create(spec)
    runner.given_deployed(spec.name, namespace=spec.namespace)

    backend.update(spec, {"primary": {"resources": {"requests": {"cpu": "50m"}}}})

    sent = runner.installed(spec.name)[-1]["values"]
    assert sent["primary"]["resources"]["requests"]["cpu"] == "50m"
    assert sent["primary"]["persistence"] == {"enabled": False}, (
        "the merge dropped a sibling key"
    )
    assert sent["architecture"] == "standalone"


def test_update_can_replace_values_outright(backend, stub_helm):
    runner = stub_helm(ValkeyDriver)
    spec = valkey(values={"architecture": "standalone", "auth": {"enabled": True}})
    backend.create(spec)
    runner.given_deployed(spec.name, namespace=spec.namespace)

    backend.update(spec, {"architecture": "replication"}, replace_values=True)

    sent = runner.installed(spec.name)[-1]["values"]
    assert sent == {"architecture": "replication"}


def test_update_of_a_missing_release_is_refused(backend, stub_helm):
    """Silently installing here would turn a typo'd name into a second
    deployment nobody asked for."""
    from multistack.cache import CacheReleaseNotFoundError

    stub_helm(ValkeyDriver)          # nothing deployed
    with pytest.raises(CacheReleaseNotFoundError, match="was not found"):
        backend.update(valkey(), {"architecture": "replication"})


def test_update_with_no_values_still_reconciles(backend, stub_helm):
    """`update(spec)` with nothing new re-applies the spec as it stands --
    the way to push a spec edit that was made directly."""
    runner = stub_helm(ValkeyDriver)
    spec = valkey()
    backend.create(spec)
    runner.given_deployed(spec.name, namespace=spec.namespace)

    backend.update(spec)

    assert len(runner.installed(spec.name)) == 2


# -- status / prerequisites ----------------------------------------------
def test_status_raises_for_a_release_that_is_not_there(backend, stub_helm):
    stub_helm(ValkeyDriver)
    with pytest.raises(Exception):
        backend.status(valkey())


def test_check_prerequisites_validates_the_spec_before_touching_anything(backend):
    """validate() runs first, so a spec that could never work fails before
    the CLI probes and the cluster round-trip."""
    with pytest.raises(ValueError):
        Cache(kubeconfig_path="", name="valkey-test", namespace="valkey-test")


# -- reporting ------------------------------------------------------------
def test_the_backend_says_what_it_is_doing(backend, stub_helm, capsys):
    """Valkey used to report through `logging`, which — with no handler
    configured, as in every example and script — meant it said nothing
    at all. A live create printed only pyhelm3's own lines, so the one
    component that was working looked like the one doing nothing. Every
    other backend prints `[prefix] ...`; this one does now too."""
    stub_helm(ValkeyDriver)
    spec = valkey()

    backend.create(spec)

    out = capsys.readouterr().out
    assert f"[valkey] deploying {spec.name}" in out
    assert f"[valkey] {spec.name} deployed" in out


def test_a_full_teardown_scopes_its_kubectl_and_names_what_it_destroyed(
    backend, stub_helm, capsys, monkeypatch
):
    """The destructive half of delete(), which nothing covered before.

    Both extra steps are irreversible, so what matters is that each is
    scoped: PVCs by the release's own instance label, and every command
    against the spec's explicit kubeconfig rather than an ambient one.
    """
    runner = stub_helm(ValkeyDriver)
    spec = valkey()
    runner.given_deployed(spec.name, namespace=spec.namespace)

    ran = []
    monkeypatch.setattr(ValkeyDriver, "_local",
                        lambda self, argv, check=True: ran.append(argv) or "")

    backend.delete(spec, delete_pvcs=True, delete_namespace=True)

    pvcs, namespace = ran
    assert pvcs[:2] == ["kubectl", "--kubeconfig"] and pvcs[2] == KUBECONFIG
    assert pvcs[3:6] == ["delete", "pvc", "-n"]
    assert f"app.kubernetes.io/instance={spec.name}" in pvcs
    assert namespace[3:5] == ["delete", "namespace"]
    assert namespace[5] == spec.namespace

    out = capsys.readouterr().out
    assert f"[valkey] uninstalled release {spec.name}" in out
    assert f"[valkey] deleted PVCs for {spec.name}" in out
    assert f"[valkey] deleted namespace {spec.namespace}" in out
