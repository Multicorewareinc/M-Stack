"""
Driving Helm.

    from multistack.helm import HelmRunner

    helm = HelmRunner("~/.multistack/kubeconfig")
    release = helm.install_or_upgrade(
        "longhorn", chart="longhorn", namespace="longhorn-system",
        # Nested, not "persistence.defaultClass". The dotted form is
        # `helm --set` syntax; pyhelm3 takes the values structure itself,
        # and a dotted key would be passed through as a literal key of
        # that name and quietly ignored by the chart.
        values={"persistence": {"defaultClass": True}},
    )

`HelmRunner` is the whole surface. It is **synchronous**, and that is the
point of this module existing.

Why a facade
------------
The layer underneath (`manager.py` -> `client.py` -> pyhelm3) is async,
because pyhelm3 is async-only. Every other part of this SDK is
synchronous: specs are pydantic models, backends are ordinary methods,
examples are scripts. Pushing async up into the backends would mean
`await` in every driver and every example, for a library that shells out
to a binary.

So the boundary stops here. `HelmRunner` runs the coroutine itself — on
this thread when nothing else is, and in a worker thread when a loop is
already running. That second case is why the bridge exists rather than a
bare `asyncio.run`: a caller inside an event loop (a FastAPI handler
calling a backend, say) would otherwise get

    RuntimeError: asyncio.run() cannot be called from a running event loop

from three frames down, having never written `await` themselves.

Mechanism, not a capability
---------------------------
Helm has no interchangeable implementations for a user to choose between,
so this is not shaped like `multistack/storage/` — no spec, no registry,
no drivers. It is mechanism, like `multistack/kube.py`: shared, decides
nothing, and imports no capability. Which chart to install and what to
call the release belong to whoever is composing.

pyhelm3 is an optional extra (`pip install -e ".[helm]"`) and is imported
only when an operation actually runs, so importing this module costs
nothing without it.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from .config import HelmConfig
from .errors import (
    HelmChartNotFoundError,
    HelmChartVersionNotFoundError,
    HelmConfigurationError,
    HelmDeploymentError,
    HelmManagerError,
    HelmOperationCancelledError,
    HelmReleaseNotFoundError,
    HelmRepositoryError,
    HelmTimeoutError,
    HelmValidationError,
)
from .models import HelmRelease, HelmRepository, ReleaseStatus
from .repositories import (
    BITNAMI_REPOSITORY,
    DEFAULT_REPOSITORIES,
    LONGHORN_REPOSITORY,
    MINIO_OPERATOR_REPOSITORY,
    PROMETHEUS_COMMUNITY_REPOSITORY,
)

# Keys whose values never reach a log or an error message.
SENSITIVE_KEYS = frozenset({
    "password", "passwd", "secret", "secretkey", "secret_key", "token",
    "apikey", "api_key", "private_key", "privatekey", "accesskey",
    "access_key", "rootpassword", "root_password",
})


def redact(values: Any) -> Any:
    """Copies `values` with sensitive leaves replaced.

    Helm values carry credentials — a MinIO tenant's root password, a
    registry pull secret — and anything that logs or reports a failed
    install will otherwise put them somewhere durable. Matching is on the
    key name, which is imperfect and still far better than nothing.
    """
    if isinstance(values, dict):
        return {
            key: ("***REDACTED***" if str(key).lower() in SENSITIVE_KEYS
                  else redact(value))
            for key, value in values.items()
        }
    if isinstance(values, (list, tuple)):
        return [redact(item) for item in values]
    return values


def run_sync(coro) -> Any:
    """Runs `coro` to completion from synchronous code.

    Uses this thread when no event loop is running, and a worker thread
    when one is. The worker case costs a thread per call and is the price
    of a sync surface over an async dependency; Helm operations take
    seconds to minutes, so it does not matter.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


class _ExpectedCommandFailure(logging.Filter):
    """Drops pyhelm3's `command failed` line, and nothing else.

    Matched on the format string rather than the rendered message, so it
    cannot be tripped by a release or namespace that happens to be named
    after the text.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return not str(record.msg).startswith("command failed")


@contextlib.contextmanager
def _quiet_expected_command_failure():
    """Silences the `command failed` warning for one call.

    Scoped to the call rather than installed once at import: suppressing
    it globally would also hide the failures that are genuinely worth
    seeing, on every other Helm operation this module runs.
    """
    log = logging.getLogger("pyhelm3.command")
    quiet = _ExpectedCommandFailure()
    log.addFilter(quiet)
    try:
        yield
    finally:
        log.removeFilter(quiet)


class HelmRunner:
    """Installs, upgrades and removes Helm releases on one cluster.

    Bound to a single kubeconfig at construction, so no call can forget
    which cluster it is acting on. `error_cls` lets a component surface
    failures as its own type — `StorageError`, `MinIOError` — while sharing
    this implementation, the same arrangement `multistack.kube` uses.
    """

    def __init__(
        self,
        kubeconfig_path: str,
        *,
        repositories: Optional[Sequence[HelmRepository]] = None,
        executable: str = "helm",
        kubecontext: Optional[str] = None,
        timeout: str = "5m",
        insecure_skip_tls_verify: bool = False,
        error_cls: Optional[type] = None,
    ) -> None:
        if not kubeconfig_path:
            raise HelmConfigurationError(
                "kubeconfig_path is required — this SDK never falls back to "
                "ambient $KUBECONFIG / ~/.kube/config resolution."
            )
        self._config = HelmConfig(
            kubeconfig=Path(kubeconfig_path),
            executable=executable,
            kubecontext=kubecontext,
            default_timeout=timeout,
            insecure_skip_tls_verify=insecure_skip_tls_verify,
        )
        self._repositories = tuple(repositories) if repositories else DEFAULT_REPOSITORIES
        self._error_cls = error_cls
        self._manager: Any = None

    # -- the async layer, built on first use ------------------------------
    @staticmethod
    def _import_manager() -> Any:
        """Split out so a test can make the import fail."""
        from .manager import HelmManager

        return HelmManager

    def _get_manager(self) -> Any:
        """The async HelmManager, imported and built lazily.

        Deferred so that importing `multistack.helm` does not require the
        `helm` extra — the same reason the driver registry names modules
        instead of importing them. `HelmRunner` can be imported and
        constructed either way; this is where the extra starts mattering.

        Which is why the ImportError is re-raised. A plain
        `pip install multistack-sdk` leaves both pyhelm3 and PyYAML out,
        and the failure a caller met was
        `ModuleNotFoundError: No module named 'yaml'` from three frames
        down — naming a transitive dependency they never asked for, and
        not the extra that installs it.
        """
        if self._manager is None:
            try:
                manager_cls = self._import_manager()
            except ImportError as exc:
                missing = getattr(exc, "name", None) or "a dependency"
                raise HelmConfigurationError(
                    f"the Helm layer needs the 'helm' extra, and {missing} is "
                    'not installed:\n\n    pip install "multistack-sdk[helm]"'
                    "\n\nIt is an extra because an RKE2-only install has no "
                    "use for it. Note helm itself still has to be on PATH — "
                    "pyhelm3 drives the binary, it does not replace it."
                ) from exc

            self._manager = manager_cls(self._config, self._repositories)
        return self._manager

    def _call(self, coro) -> Any:
        """Runs a coroutine, re-typing failures if asked to."""
        try:
            return run_sync(coro)
        except HelmManagerError as exc:
            if self._error_cls is None:
                raise
            raise self._error_cls(str(exc)) from exc

    # -- operations -------------------------------------------------------
    def install_or_upgrade(
        self,
        release: str,
        *,
        chart: Optional[str] = None,
        accelerator_type: Optional[str] = None,
        namespace: str,
        values: Optional[Dict[str, Any]] = None,
        chart_version: Optional[str] = None,
        atomic: bool = True,
        wait: bool = True,
        create_namespace: bool = True,
        strict_values: bool = True,
    ) -> HelmRelease:
        """Installs `chart` as `release`, upgrading it if already present.

        One method rather than install/upgrade, because every caller in
        this SDK wants `helm upgrade --install` semantics: re-running a
        provisioning script must not fail because it already ran.

        `atomic` defaults on, so a release that does not come up is rolled
        back rather than left half-installed for someone to find later.
        """
        manager = self._get_manager()
        result = self._call(manager.install(
            release,
            chart,
            accelerator_type=accelerator_type,
            chart_version=chart_version,
            namespace=namespace,
            values=values or {},
            wait=wait,
            atomic=atomic,
            create_namespace=create_namespace,
            strict_values=strict_values,
        ))
        return result.release

    def uninstall(
        self,
        release: str,
        *,
        namespace: str,
        wait: bool = True,
        keep_history: bool = False,
        missing_ok: bool = False,
    ) -> None:
        """Removes a release. Raises unless `missing_ok` when there is none.

        `missing_ok` is what a `delete()` that should be idempotent passes;
        without it, "there was nothing to remove" is reported rather than
        swallowed.
        """
        self._call(self._get_manager().uninstall(
            release,
            namespace=namespace,
            wait=wait,
            keep_history=keep_history,
            missing_ok=missing_ok,
        ))

    def status(self, release: str, *, namespace: str) -> HelmRelease:
        """The release's current revision and status."""
        return self._call(self._get_manager().status(release, namespace=namespace))

    def get_release(
        self, release: str, *, namespace: str
    ) -> Optional[HelmRelease]:
        """The release as it stands, or None when there is none.

        `status()` raises for a missing release; this returns None, for
        the caller who is asking whether one exists at all.

        The absence probe runs `helm status`, which exits non-zero when
        there is no release -- and pyhelm3 logs every non-zero exit as
        `command failed: helm status ...` at WARNING. Here that line is
        describing the expected answer, so it is suppressed: a `delete()`
        against an already-absent release printed four of them, and a
        clean run that reads as four failures teaches people to ignore
        the word "failed". Only this one message, only for the duration
        of the probe -- a real failure still carries its own exception,
        with the stderr in it.
        """
        with _quiet_expected_command_failure():
            return self._call(
                self._get_manager().get_release(release, namespace=namespace)
            )

    def is_deployed(self, release: str, *, namespace: str) -> bool:
        """Whether the release exists and its last operation succeeded.

        The question a `create()` asks before deciding whether it has
        anything to do. A release in `failed` counts as not deployed:
        Helm refuses to upgrade over one, so a caller has to know.

        Anything other than absence still raises. An unreachable cluster
        or a missing helm binary is not an answer to this question, and
        returning False for those made a broken connection look like a
        clean slate — which a caller then acts on by installing.

        This asks `get_release()` rather than catching `status()`, because
        `_call()` re-types errors to `error_cls` when a component set one:
        by the time the exception arrived here it was a `StorageError`, so
        the not-found case had to be recovered by finding "was not found"
        in the message. Deciding control flow on message text meant any
        other failure that happened to contain that phrase read as a clean
        slate. Absence is now established below the re-typing, and this
        method matches on None.
        """
        found = self.get_release(release, namespace=namespace)
        return found is not None and found.status is ReleaseStatus.DEPLOYED


__all__ = [
    "HelmRunner",
    "HelmRelease",
    "HelmRepository",
    "ReleaseStatus",
    "redact",
    "run_sync",
    "DEFAULT_REPOSITORIES",
    "LONGHORN_REPOSITORY",
    "NATS_REPOSITORY",
    "MINIO_OPERATOR_REPOSITORY",
    "PROMETHEUS_COMMUNITY_REPOSITORY",
    "BITNAMI_REPOSITORY",
    # Errors, so a caller can handle failures without importing internals.
    "HelmManagerError",
    "HelmChartNotFoundError",
    "HelmChartVersionNotFoundError",
    "HelmConfigurationError",
    "HelmDeploymentError",
    "HelmOperationCancelledError",
    "HelmReleaseNotFoundError",
    "HelmRepositoryError",
    "HelmTimeoutError",
    "HelmValidationError",
]
