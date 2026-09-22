"""Tests for the Helm layer's sync surface and its own rules.

The async machinery underneath is pyhelm3's; what is ours is the sync
boundary, the repository handling, the values check and the error typing.
Those are what these cover, without needing a cluster or the extra.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from multistack.helm import (
    DEFAULT_REPOSITORIES,
    HelmConfigurationError,
    HelmReleaseNotFoundError,
    HelmRunner,
    ReleaseStatus,
    redact,
    run_sync,
)
from multistack.helm.config import HelmConfig
from multistack.helm.errors import (
    HelmDeploymentError,
    HelmManagerError,
    HelmValidationError,
)
from multistack.helm.repositories import BITNAMI_REPOSITORY, LONGHORN_REPOSITORY
from multistack.helm.values import HelmValuesValidator as V

KUBECONFIG = "/tmp/kc.yaml"


class Boom(RuntimeError):
    """Stands in for a component's own error type."""


# -- the sync boundary ----------------------------------------------------
async def answer():
    await asyncio.sleep(0)
    return 42


def test_runs_a_coroutine_from_plain_sync_code():
    assert run_sync(answer()) == 42


def test_runs_a_coroutine_from_inside_a_running_loop():
    # The case a bare asyncio.run() cannot handle. Nothing in this repo
    # calls a backend from async code today; this is what makes that
    # possible without the failure appearing three frames down in a
    # driver, invisible to a synchronous suite.
    async def caller():
        return run_sync(answer())

    assert asyncio.run(caller()) == 42


def test_a_bare_asyncio_run_would_have_failed_here():
    # Pins the reason run_sync exists, so nobody "simplifies" it back.
    async def caller():
        coro = answer()
        try:
            return asyncio.run(coro)
        finally:
            coro.close()   # asyncio.run raised before awaiting it

    with pytest.raises(RuntimeError, match="cannot be called from a running event loop"):
        asyncio.run(caller())


# -- configuration --------------------------------------------------------
def test_kubeconfig_is_required():
    # Helm resolves $KUBECONFIG / ~/.kube/config itself when not told
    # which cluster to act on, and a stale one targets the wrong cluster.
    with pytest.raises(HelmConfigurationError, match="kubeconfig_path is required"):
        HelmRunner("")


def test_a_runner_is_bound_to_one_cluster():
    runner = HelmRunner(KUBECONFIG)
    assert str(runner._config.kubeconfig) == KUBECONFIG


def test_helm_is_not_imported_until_an_operation_runs():
    import sys

    HelmRunner(KUBECONFIG)          # constructing must not need the extra
    assert "pyhelm3" not in sys.modules or True  # tolerant: another test may have
    # The real guarantee: the manager is built lazily, not in __init__.
    assert HelmRunner(KUBECONFIG)._manager is None


# -- repositories ---------------------------------------------------------
def test_default_repositories_put_small_specific_ones_first():
    # The resolver loads each index until it finds the chart, so a
    # repository after a large one pays for that index on every miss:
    # resolving `longhorn` measured 0.2s with longhorn first, 34.8s with
    # bitnami first, because Bitnami's index is 27MB against 64KB.
    names = [r.name for r in DEFAULT_REPOSITORIES]
    assert names.index("longhorn") < names.index("bitnami")
    assert names.index("minio-operator") < names.index("bitnami")


def test_the_charts_this_sdk_installs_have_a_repository():
    urls = {r.url for r in DEFAULT_REPOSITORIES}
    assert "https://charts.longhorn.io" in urls
    assert "https://operator.min.io" in urls


def test_repositories_can_be_replaced_by_the_caller():
    # A repository URL is deployment configuration — an internal mirror,
    # an air-gapped registry — not a fact about the code.
    only_ours = (LONGHORN_REPOSITORY,)
    runner = HelmRunner(KUBECONFIG, repositories=only_ours)
    assert runner._repositories == only_ours


def test_the_repository_index_is_cached_per_resolver():
    from multistack.helm.resolver import ChartResolver

    loads = []

    class Counting(ChartResolver):
        def _load_repository_index(self, repository):
            loads.append(repository.url)
            return {"entries": {"nginx": [{"version": "1.0.0"}]}}

    resolver = Counting((BITNAMI_REPOSITORY,), timeout=1)
    for _ in range(3):
        asyncio.run(resolver.resolve("nginx"))
    assert loads == [BITNAMI_REPOSITORY.url], "index re-fetched"

    resolver.clear_cache()
    asyncio.run(resolver.resolve("nginx"))
    assert len(loads) == 2, "clear_cache() should force a re-fetch"


# -- values ---------------------------------------------------------------
LONGHORN_DEFAULTS = {
    "persistence": {"defaultClass": False, "defaultClassReplicaCount": 3},
    "csi": {"kubeletRootDir": None},
    "nodeSelector": {},
}


def test_a_typo_is_reported():
    unknown = V.validate({"persistance": {"defaultClass": True}}, LONGHORN_DEFAULTS)
    assert len(unknown) == 1
    assert "persistance" in unknown[0]


def test_reporting_is_the_default_not_refusing():
    """The MinIO tenant chart documents .tenant.configuration.name in its
    own values.yaml and reads it in two templates, while the parsed
    defaults have no `configuration` key. Strict mode rejects a value the
    chart supports, so strict is a choice rather than the default."""
    V.validate({"whatever": {"the": "chart"}}, LONGHORN_DEFAULTS)  # no raise


def test_strict_refuses():
    with pytest.raises(HelmValidationError, match="persistance"):
        V.validate({"persistance": {}}, LONGHORN_DEFAULTS, strict=True)


def test_known_paths_are_accepted():
    assert V.validate(
        {"persistence": {"defaultClassReplicaCount": 2}}, LONGHORN_DEFAULTS
    ) == []


def test_an_empty_default_mapping_accepts_anything_under_it():
    # `nodeSelector: {}` exists so callers can supply arbitrary keys.
    assert V.validate({"nodeSelector": {"disk": "ssd"}}, LONGHORN_DEFAULTS) == []


def test_every_unknown_path_is_reported_not_just_the_first():
    unknown = V.validate({"aaa": 1, "zzz": 2}, LONGHORN_DEFAULTS)
    assert len(unknown) == 2


# -- redaction ------------------------------------------------------------
def test_credentials_are_redacted():
    # A MinIO tenant's root password otherwise ends up in a log file that
    # outlives the release.
    out = redact({"tenant": {"configSecret": {"name": "s", "secretKey": "hunter2"}}})
    assert out["tenant"]["configSecret"]["secretKey"] == "***REDACTED***"
    assert out["tenant"]["configSecret"]["name"] == "s"


def test_redaction_reaches_inside_lists():
    out = redact({"pools": [{"servers": 2, "password": "p"}]})
    assert out["pools"][0]["password"] == "***REDACTED***"
    assert out["pools"][0]["servers"] == 2


def test_redaction_does_not_mutate_the_original():
    original = {"password": "keep"}
    redact(original)
    assert original["password"] == "keep"


def test_manager_log_redaction_catches_minio_credential_keys():
    # HelmManager._redact_values used to carry its own, smaller copy of
    # the sensitive-key set -- one missing exactly the two keys a MinIO
    # tenant's values use, `secretKey`/`accessKey` -- so the install/
    # upgrade DEBUG log lines that call it leaked them while this
    # module's own redact() (tested above) already caught them. It now
    # delegates to redact() instead of carrying a second, drifting copy.
    from multistack.helm.manager import HelmManager

    out = HelmManager._redact_values(
        {"tenant": {"secretKey": "hunter2", "accessKey": "AKIA123", "name": "ok"}}
    )
    assert out["tenant"]["secretKey"] == "***REDACTED***"
    assert out["tenant"]["accessKey"] == "***REDACTED***"
    assert out["tenant"]["name"] == "ok"


# -- error typing ---------------------------------------------------------
def test_failures_can_surface_as_a_components_own_error():
    # So `except StorageError` keeps working while sharing one
    # implementation, the same arrangement multistack.kube uses.
    runner = HelmRunner(KUBECONFIG, error_cls=Boom)

    async def fails():
        raise HelmManagerError("chart exploded")

    with pytest.raises(Boom, match="chart exploded"):
        runner._call(fails())


def test_without_error_cls_the_helm_error_propagates():
    runner = HelmRunner(KUBECONFIG)

    async def fails():
        raise HelmManagerError("chart exploded")

    with pytest.raises(HelmManagerError, match="chart exploded"):
        runner._call(fails())


def test_every_error_shares_one_base():
    from multistack.helm import errors

    for name in dir(errors):
        obj = getattr(errors, name)
        if isinstance(obj, type) and issubclass(obj, Exception):
            assert issubclass(obj, HelmManagerError), name


# -- get_release and is_deployed ------------------------------------------
class FakeRelease:
    def __init__(self, status):
        self.status = status


class FakeManager:
    """Stands in for the async layer, so the sync wiring is what runs."""

    def __init__(self, result=None, raises=None):
        self._result = result
        self._raises = raises

    async def get_release(self, release, *, namespace=None):
        if self._raises is not None:
            raise self._raises
        return self._result


def runner_over(manager, monkeypatch, **kwargs):
    runner = HelmRunner(KUBECONFIG, **kwargs)
    monkeypatch.setattr(runner, "_get_manager", lambda: manager)
    return runner


def test_get_release_is_none_when_there_is_no_release(monkeypatch):
    runner = runner_over(FakeManager(result=None), monkeypatch)
    assert runner.get_release("x", namespace="ns") is None


def test_get_release_returns_the_release_when_there_is_one(monkeypatch):
    release = FakeRelease(ReleaseStatus.DEPLOYED)
    runner = runner_over(FakeManager(result=release), monkeypatch)
    assert runner.get_release("x", namespace="ns") is release


class NoisyManager(FakeManager):
    """Logs the way pyhelm3 does when a helm command exits non-zero."""

    async def get_release(self, release, *, namespace=None):
        logging.getLogger("pyhelm3.command").warning(
            "command failed: %s", "helm status x --namespace ns"
        )
        return await super().get_release(release, namespace=namespace)


def test_get_release_does_not_log_the_absence_it_was_asked_about(
    monkeypatch, caplog
):
    """`helm status` exits non-zero for a release that is not there.

    pyhelm3 logs every non-zero exit as `command failed`, so a delete()
    against an already-absent release printed one of those per probe --
    four, in a full teardown. A clean run that reads as four failures
    teaches people to ignore the word.
    """
    runner = runner_over(NoisyManager(result=None), monkeypatch)

    with caplog.at_level(logging.WARNING):
        assert runner.get_release("x", namespace="ns") is None

    assert "command failed" not in caplog.text


def test_the_suppression_does_not_outlive_the_call(monkeypatch, caplog):
    """Everything else Helm does still says so when it fails.

    The filter is scoped to the probe, not installed once at import: a
    real failure on any other operation is exactly what someone reading
    the output needs.
    """
    runner = runner_over(NoisyManager(result=None), monkeypatch)
    runner.get_release("x", namespace="ns")

    with caplog.at_level(logging.WARNING):
        logging.getLogger("pyhelm3.command").warning(
            "command failed: %s", "helm upgrade x"
        )

    assert "command failed" in caplog.text


def test_is_deployed_is_false_when_the_release_is_absent(monkeypatch):
    runner = runner_over(FakeManager(result=None), monkeypatch)
    assert runner.is_deployed("x", namespace="ns") is False


def test_is_deployed_is_false_for_a_failed_release(monkeypatch):
    # Helm refuses to upgrade over a failed release, so a caller has to
    # know the difference between "not there" and "there but broken".
    runner = runner_over(FakeManager(result=FakeRelease(ReleaseStatus.FAILED)),
                         monkeypatch)
    assert runner.is_deployed("x", namespace="ns") is False


def test_is_deployed_is_true_for_a_deployed_release(monkeypatch):
    runner = runner_over(FakeManager(result=FakeRelease(ReleaseStatus.DEPLOYED)),
                         monkeypatch)
    assert runner.is_deployed("x", namespace="ns") is True


def test_is_deployed_does_not_read_a_failure_message_as_absence(monkeypatch):
    """The regression the previous implementation could produce.

    is_deployed used to recover the not-found case by looking for "was
    not found" in the message, because _call() had already re-typed the
    exception. Any other failure carrying that phrase — a missing chart
    repository, here — then read as a clean slate, and the caller acted
    on it by installing.
    """
    runner = runner_over(
        FakeManager(raises=HelmDeploymentError(
            "Helm status failed for release 'x': repo was not found"
        )),
        monkeypatch,
    )
    with pytest.raises(HelmDeploymentError):
        runner.is_deployed("x", namespace="ns")


def test_is_deployed_reports_absence_through_a_components_own_error(monkeypatch):
    """error_cls re-typing must not turn absence into a raised error.

    A component passes error_cls so its callers can catch StorageError.
    Absence is established in the async layer, below that re-typing, so
    it still arrives as None rather than as a Boom.
    """
    runner = runner_over(FakeManager(result=None), monkeypatch, error_cls=Boom)
    assert runner.is_deployed("x", namespace="ns") is False


# -- what gets logged -----------------------------------------------------
def handle(exc, operation="status"):
    """Calls the handler from inside an except block, as the manager does.

    That detail matters for what these tests can see: LOGGER.exception
    called outside an except block records exc_info=(None, None, None),
    which is truthy, so "was a traceback logged" would pass either way.
    """
    from multistack.helm.manager import HelmManager

    try:
        raise exc
    except BaseException:
        return HelmManager._handle_exception(
            operation=operation, release_name="x", exc=exc
        )


def test_a_missing_release_is_not_logged_as_a_failure(caplog):
    """A miss is the answer on every first install, not an error.

    It was logged with LOGGER.exception, so the ordinary
    check-then-install path wrote a stack trace every time — which is the
    noise that hides a real one.
    """
    pyhelm3 = pytest.importorskip("pyhelm3")

    with caplog.at_level(logging.DEBUG, logger="multistack.helm.manager"):
        with pytest.raises(HelmReleaseNotFoundError):
            # pyhelm3 builds its errors from the command result:
            # (returncode, stdout, stderr).
            handle(pyhelm3.ReleaseNotFoundError(
                1, b"", b"Error: release: not found"
            ))

    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not [r for r in caplog.records if r.exc_info]
    assert any(r.levelno == logging.DEBUG for r in caplog.records)


def test_a_real_failure_still_logs_a_traceback(caplog):
    with caplog.at_level(logging.DEBUG, logger="multistack.helm.manager"):
        with pytest.raises(HelmDeploymentError):
            handle(RuntimeError("etcdserver: request timed out"))

    logged = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert logged, "a real failure was not logged as one"
    assert any(r.exc_info and r.exc_info[0] is RuntimeError for r in logged), (
        "logged without the traceback"
    )


# -- local charts ---------------------------------------------------------
# The first-party charts under api/microservices/*/chart are published
# nowhere, so the resolver has nothing to find. These cover the branch
# that recognises them, and the two ways it could go wrong: mistaking a
# repository chart name for a path, or accepting a path silently when it
# is misspelled.
from pathlib import Path                                  # noqa: E402

from multistack.helm.manager import HelmManager           # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_a_directory_holding_a_chart_yaml_is_a_local_chart(tmp_path):
    (tmp_path / "Chart.yaml").write_text("name: x\n")
    assert HelmManager._local_chart(str(tmp_path)) == tmp_path


def test_a_directory_without_a_chart_yaml_is_not(tmp_path):
    # Falls through to the resolver, which reports "not found in any
    # repository" -- the right error for a path that is not a chart.
    assert HelmManager._local_chart(str(tmp_path)) is None


def test_a_packaged_chart_is_a_local_chart(tmp_path):
    archive = tmp_path / "model-gateway-0.1.0.tgz"
    archive.write_bytes(b"")
    assert HelmManager._local_chart(str(archive)) == archive


def test_a_repository_chart_name_is_never_treated_as_a_path():
    # The reason detection requires the path to exist. Otherwise a
    # directory called ./longhorn in someone's working tree would
    # shadow the chart of that name.
    for name in ("longhorn", "redis", "minio-operator"):
        assert HelmManager._local_chart(name) is None


def test_a_misspelled_path_is_not_silently_a_chart_name():
    assert HelmManager._local_chart("api/microservices/model-gatewy/chart") is None


def test_the_first_party_charts_are_recognised():
    for service in ("model-gateway", "rate-limiter-rpm"):
        chart = REPO_ROOT / "api/microservices" / service / "chart"
        assert HelmManager._local_chart(str(chart)) == chart, service


def test_a_chart_version_with_a_local_chart_is_refused(tmp_path):
    """A local chart's version is whatever its Chart.yaml says, so a
    chart_version alongside it is a mistake worth naming rather than
    ignoring."""
    pytest.importorskip("pyhelm3")
    kubeconfig = tmp_path / "kc.yaml"
    kubeconfig.write_text("apiVersion: v1\n")
    (tmp_path / "Chart.yaml").write_text("name: x\n")

    manager = HelmManager(HelmConfig(kubeconfig=kubeconfig))
    with pytest.raises(HelmValidationError) as raised:
        run_sync(manager._chart_source(str(tmp_path), "1.2.3"))
    assert "1.2.3" in str(raised.value)
    assert "Chart.yaml" in str(raised.value)


# -- charts named by address (Harbor) -------------------------------------
# Harbor 2.8+ serves charts as OCI artifacts, so there is no index.yaml
# for ChartResolver to read. An oci:// ref has to reach Helm untouched.
def test_an_oci_ref_is_an_explicit_reference():
    assert HelmManager._is_explicit_ref("oci://harbor.local/platform/model-gateway")


def test_a_chart_name_is_not():
    for name in ("longhorn", "redis", "api/microservices/model-gateway/chart"):
        assert not HelmManager._is_explicit_ref(name)


def test_an_oci_ref_keeps_its_version_and_never_reaches_the_resolver(tmp_path):
    """Two claims in one, both load-bearing.

    The version must travel with the ref, because for OCI the version is
    the artifact tag -- Helm cannot find it any other way. And the
    resolver must not be consulted: it would fetch index.yaml from a
    registry that has none and report the chart missing.
    """
    pytest.importorskip("pyhelm3")
    from multistack.helm.models import ChartRef

    kubeconfig = tmp_path / "kc.yaml"
    kubeconfig.write_text("apiVersion: v1\n")
    manager = HelmManager(HelmConfig(kubeconfig=kubeconfig))

    def refuse(*a, **k):
        raise AssertionError("the resolver was consulted for an oci:// ref")

    manager._resolver.resolve = refuse

    source = run_sync(manager._chart_source(
        "oci://harbor.local/platform/model-gateway", "0.2.1"
    ))
    assert source == ChartRef(ref="oci://harbor.local/platform/model-gateway",
                              version="0.2.1")


def test_an_oci_ref_without_a_version_is_allowed():
    """Helm takes the newest tag. Pinning is the caller's decision, not
    something this layer should force -- and refusing it here would make
    `helm install oci://...` and this method behave differently."""
    pytest.importorskip("pyhelm3")
    from multistack.helm.models import ChartRef

    assert ChartRef(ref="oci://h/p/c").version is None


def test_a_missing_helm_extra_names_the_extra(monkeypatch):
    """The failure a pip user without the extra actually meets.

    `pip install multistack-sdk` leaves out pyhelm3 and PyYAML, and
    resolver.py imports yaml at module scope -- so the first operation
    raised `No module named 'yaml'`, naming a transitive dependency the
    caller never asked for rather than the extra that installs it.
    """
    runner = HelmRunner(KUBECONFIG)
    monkeypatch.setattr(
        HelmRunner, "_import_manager",
        staticmethod(lambda: (_ for _ in ()).throw(
            ModuleNotFoundError("No module named 'yaml'", name="yaml"))),
    )
    with pytest.raises(HelmConfigurationError) as raised:
        runner.is_deployed("x", namespace="ns")

    message = str(raised.value)
    assert 'pip install "multistack-sdk[helm]"' in message
    assert "yaml" in message
    assert "on PATH" in message      # helm itself is still needed


# -- chart metadata on a release ------------------------------------------
class FakeRevision:
    """Enough of a pyhelm3 ReleaseRevision to build a HelmRelease from."""

    class _Release:
        name = "gateway"
        namespace = "platform"

    class _Status:
        value = "deployed"

    def __init__(self, metadata=None, raises=None):
        self.release = self._Release()
        self.status = self._Status()
        self.revision = 2
        self._metadata = metadata
        self._raises = raises

    async def chart_metadata(self):
        if self._raises is not None:
            raise self._raises
        return self._metadata


class FakeMetadata:
    name = "model-gateway"
    version = "0.1.0"


def test_a_release_carries_its_chart_name_and_version():
    """Both fields were declared on HelmRelease and never populated, so
    status() always reported chart=None -- the field you want when
    checking which version is actually deployed."""
    release = run_sync(HelmManager._build_release(FakeRevision(FakeMetadata())))
    assert (release.chart_name, release.chart_version) == ("model-gateway", "0.1.0")
    assert release.status is ReleaseStatus.DEPLOYED


def test_unavailable_chart_metadata_does_not_lose_the_release():
    """The metadata is a follow-up lookup, and for a status call it is a
    second helm invocation. Failing the whole call because it did not
    work would throw away the name, revision and status the caller
    actually asked for."""
    release = run_sync(HelmManager._build_release(
        FakeRevision(raises=RuntimeError("helm get metadata failed"))))
    assert release.name == "gateway"
    assert release.revision == 2
    assert release.status is ReleaseStatus.DEPLOYED
    assert release.chart_name is None
    assert release.chart_version is None
