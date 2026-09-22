"""Valkey, installed from the Bitnami chart via the Helm layer.

Helm lifecycle operations are delegated to the common `HelmRunner`.
Kubernetes operations use the shared CLI execution helpers and are
always scoped to the spec's explicit `kubeconfig_path`.

Ported from `backends/valkey_client.py` unchanged apart from its shape:
the state-tracking decorators moved up to `CacheBackend` in
`registry.py`, where every migrated capability keeps them, and the
release identity now reads `resolved_namespace` off the spec rather than
a bare `namespace` field.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ...helm import HelmRelease, HelmRunner
from ...kube import require_cli, require_cluster, run_local
from ..base import CacheError, CachePrerequisiteError, CacheReleaseNotFoundError
from ..spec import Cache


class ValkeyDriver:
    """Creates and manages Valkey releases on an existing cluster."""

    # Named for the implementation, not the capability, following
    # storage's `[longhorn]` -- it tells a reader which cache is
    # talking. Also means migrating changed no visible output.
    log_prefix = "valkey"

    def __init__(self, command_timeout: int = 600) -> None:
        self.command_timeout = command_timeout

    # -- prerequisites ------------------------------------------------------

    def check_prerequisites(self, cache: Cache) -> None:
        """Verify the required CLIs exist and the cluster is reachable."""

        cache.validate()

        for cli in ("helm", "kubectl"):
            self._require_cli(cli)

        require_cluster(cache.kubeconfig_path, capability="a cache")

    # -- lifecycle ----------------------------------------------------------

    def create(self, cache: Cache) -> HelmRelease:
        """Install or upgrade the release.

        Idempotent: an existing healthy release is upgraded in place.
        """

        self.check_prerequisites(cache)

        self._deploy(cache)

        release = self._find_release(cache)

        if release is None:
            raise CacheError(
                f"Cache release '{cache.name}' could not be found after "
                "deployment."
            )

        return release

    def update(
        self,
        cache: Cache,
        values: Optional[Dict[str, Any]] = None,
        *,
        replace_values: bool = False,
    ) -> HelmRelease:
        """Update an existing release.

        Values are deep-merged with the current ones by default; set
        `replace_values=True` to replace the dictionary outright.
        """

        self.check_prerequisites(cache)

        if self._find_release(cache) is None:
            raise CacheReleaseNotFoundError(
                f"Cache release '{cache.name}' was not found in namespace "
                f"'{cache.resolved_namespace}'."
            )

        if values is not None:
            cache.update_values(values, replace=replace_values)

        self._deploy(cache)

        release = self._find_release(cache)

        if release is None:
            raise CacheError(
                f"Cache release '{cache.name}' could not be found after "
                "update."
            )

        return release

    def delete(
        self,
        cache: Cache,
        delete_pvcs: bool = False,
        delete_namespace: bool = False,
    ) -> None:
        """Uninstall the release.

        PVCs and the namespace are preserved unless explicitly asked
        for -- a cache that comes back without its volume is a different
        thing from one that comes back.
        """

        cache.validate()
        self._require_cli("helm")

        namespace = cache.resolved_namespace

        if self._find_release(cache) is not None:
            runner = self._helm_runner(cache)

            runner.uninstall(cache.name, namespace=namespace, missing_ok=True)

            print(
                f"[{self.log_prefix}] uninstalled release {cache.name} "
                f"from namespace {namespace}"
            )

        if delete_pvcs:
            self._delete_pvcs(cache)
            print(f"[{self.log_prefix}] deleted PVCs for {cache.name}")

        if delete_namespace:
            self._delete_namespace(cache)
            print(f"[{self.log_prefix}] deleted namespace {namespace}")

    # -- release information -----------------------------------------------

    def exists(self, cache: Cache) -> bool:
        """Whether the Helm release exists."""

        cache.validate()

        return self._find_release(cache) is not None

    def status(self, cache: Cache) -> HelmRelease:
        """The current Helm release information."""

        cache.validate()

        release = self._find_release(cache)

        if release is None:
            raise CacheReleaseNotFoundError(
                f"Cache release '{cache.name}' was not found in namespace "
                f"'{cache.resolved_namespace}'."
            )

        return release

    # -- internals ----------------------------------------------------------

    def _deploy(self, cache: Cache) -> None:
        """Install or upgrade the release.

        A failed existing release is removed first, so Helm can perform a
        clean installation rather than trying to upgrade a broken one.
        """

        runner = self._helm_runner(cache)
        namespace = cache.resolved_namespace

        existing = self._find_release(cache)

        if existing is not None and existing.status.value == "failed":
            print(
                f"[{self.log_prefix}] clearing failed release {cache.name} "
                f"in namespace {namespace}"
            )

            runner.uninstall(cache.name, namespace=namespace)

        print(
            f"[{self.log_prefix}] deploying {cache.name} "
            f"in namespace {namespace}"
        )

        runner.install_or_upgrade(
            cache.name,
            chart=cache.chart,
            namespace=namespace,
            values=cache.helm_values(),
            chart_version=cache.chart_version,
        )

        print(f"[{self.log_prefix}] {cache.name} deployed")

    def _find_release(
        self,
        cache: Cache,
        name: Optional[str] = None,
        namespace: Optional[str] = None,
    ) -> Optional[HelmRelease]:
        """The matching Helm release, or None when absent."""

        name = name or cache.name
        namespace = namespace or cache.resolved_namespace

        runner = self._helm_runner(cache)

        return runner.get_release(name, namespace=namespace)

    def _delete_pvcs(self, cache: Cache) -> None:
        """Delete PVCs belonging to this release."""

        self._require_cli("kubectl")

        self._kubectl(
            cache,
            "delete", "pvc",
            "-n", cache.resolved_namespace,
            "-l", f"app.kubernetes.io/instance={cache.name}",
            "--ignore-not-found",
        )

    def _delete_namespace(self, cache: Cache) -> None:
        """Delete the namespace this deployed into."""

        self._require_cli("kubectl")

        self._kubectl(
            cache,
            "delete", "namespace", cache.resolved_namespace,
            "--ignore-not-found",
        )

    def _helm_runner(self, cache: Cache) -> HelmRunner:
        """A HelmRunner bound to the target cluster."""

        return HelmRunner(cache.kubeconfig_path)

    def _kubectl(self, cache: Cache, *args: str, check: bool = True) -> str:
        """Run kubectl against this deployment's cluster."""

        return self._local(
            ["kubectl", "--kubeconfig", cache.kubeconfig_path, *args],
            check=check,
        )

    def _local(self, argv: List[str], check: bool = True) -> str:
        """Run a local CLI command with this capability's error type."""

        return run_local(
            argv,
            check=check,
            timeout=self.command_timeout,
            error_cls=CacheError,
        )

    def _require_cli(self, name: str) -> None:
        """Verify a required CLI exists without executing it."""

        require_cli(
            name,
            error_cls=CachePrerequisiteError,
            purpose="this driver drives it directly",
        )
