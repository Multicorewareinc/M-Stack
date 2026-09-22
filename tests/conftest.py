"""Fixtures every test in this tree gets.

The state layer records each backend `create()` and `delete()` into a
SQLite file, which `default_state_manager()` resolves from
MULTISTACK_STATE_DB or, failing that, `~/.multistack/state.db`. Left
alone the suite writes *test fixture names* into the developer's real
state file as healthy deployments — `shrink`, `twice`,
`early-kubeconfig` and friends all turned up there — and two things then
go wrong:

  1. The state DB stops being a record of what is deployed, which is the
     one job it has.
  2. A test's result depends on the machine. `@track_create` refuses to
     run when a spec's REQUIRES has no healthy row, so a developer who
     has genuinely deployed storage through the SDK sees the MinIO tests
     pass while CI, with an empty DB, sees them fail.

Autouse, because the tracking decorators fire inside any backend
`create()` and there is no way to know in advance which test reaches
one.

This mirrors the module-local `isolated_state` fixture in
`tests/test_backend_state_tracking.py`, including the singleton reset:
`default_state_manager()` caches its StateManager on first use, so
without clearing it a later test keeps writing to an earlier test's
file and the env var has no effect.
"""
from __future__ import annotations

import pytest

from multistack.state import tracking


@pytest.fixture(autouse=True)
def isolated_state_db(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTISTACK_STATE_DB", str(tmp_path / "state.db"))
    tracking._default = None
    yield
    tracking._default = None


# ---------------------------------------------------------------- helm --
# Backends that install charts used to shell out through `backend._local`,
# so their tests stubbed that one method and never touched a cluster. The
# pyhelm ports (MinIO, Valkey, and Longhorn next) call `HelmRunner`
# instead, which does not go through `_local` -- so the old stub stops
# applying and pyhelm3 goes looking for a real cluster:
#
#     pyhelm3.errors.Error: Kubernetes cluster unreachable: stat /tmp/kc.yaml
#
# The seam below replaces it. Both ported backends build their runner in a
# `_helm_runner(spec)` method, so patching that one method is enough, and
# it is the only thing a future port has to keep doing for its tests to
# work the same way.

from multistack.helm import HelmRelease, ReleaseStatus  # noqa: E402


class RecordingHelmRunner:
    """A HelmRunner that records calls instead of reaching a cluster.

    Deliberately mirrors the real signatures rather than accepting
    `*args, **kwargs`: a stub that takes anything keeps passing after the
    real API changes under it, which is worse than no stub at all. If
    HelmRunner grows a parameter, this fails and someone looks.
    """

    def __init__(self):
        self.installs = []
        self.uninstalls = []
        # release name -> HelmRelease, for get_release/is_deployed.
        self.releases = {}

    # -- what the backends call --------------------------------------
    def install_or_upgrade(
        self, release, *, chart=None, namespace, values=None,
        chart_version=None, create_namespace=True, strict_values=True,
        **extra,
    ):
        self.installs.append({
            "release": release, "chart": chart, "namespace": namespace,
            "values": values or {}, "chart_version": chart_version,
            "create_namespace": create_namespace,
            "strict_values": strict_values, **extra,
        })
        return self.releases.setdefault(
            release, self._release(release, namespace))

    def uninstall(self, release, *, namespace, missing_ok=False, **extra):
        self.uninstalls.append({
            "release": release, "namespace": namespace,
            "missing_ok": missing_ok, **extra,
        })
        self.releases.pop(release, None)

    def get_release(self, release, *, namespace):
        return self.releases.get(release)

    def status(self, release, *, namespace):
        found = self.releases.get(release)
        if found is None:
            raise KeyError(f"no release named {release!r}")
        return found

    def is_deployed(self, release, *, namespace):
        found = self.releases.get(release)
        return found is not None and found.status == ReleaseStatus.DEPLOYED

    # -- test-side helpers -------------------------------------------
    def given_deployed(self, release, *, namespace=None, revision=1):
        """Pretend a release is already installed."""
        self.releases[release] = self._release(release, namespace, revision)
        return self.releases[release]

    def installed(self, release):
        """Every install recorded for one release, in order."""
        return [c for c in self.installs if c["release"] == release]

    @staticmethod
    def _release(name, namespace, revision=1):
        return HelmRelease(
            name=name, namespace=namespace, revision=revision,
            status=ReleaseStatus.DEPLOYED,
        )


@pytest.fixture
def helm_runner():
    """The recording runner on its own, for asserting against."""
    return RecordingHelmRunner()


@pytest.fixture
def stub_helm(monkeypatch, helm_runner):
    """Point a `_helm_runner()` at the recording runner.

        def test_something(backend, stub_helm):
            runner = stub_helm(backend)
            backend.create(spec)
            assert runner.installed("minio-operator")

    Takes an instance for the `core/` + `backends/` components, which
    own `_helm_runner` directly, or a driver *class* for the migrated
    ones, where `CapabilityBackend` constructs and caches the driver
    itself so there is no instance to reach for:

            runner = stub_helm(ValkeyDriver)
    """
    def install(target):
        if isinstance(target, type):
            monkeypatch.setattr(
                target, "_helm_runner", lambda self, *a, **k: helm_runner)
        else:
            monkeypatch.setattr(
                target, "_helm_runner", lambda *a, **k: helm_runner)
        return helm_runner
    return install
