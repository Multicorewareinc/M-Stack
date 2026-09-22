from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, NoReturn, Optional, Sequence

from .config import HelmConfig
from .errors import (
    HelmConfigurationError,
    HelmDeploymentError,
    HelmOperationCancelledError,
    HelmReleaseNotFoundError,
    HelmTimeoutError,
    HelmValidationError,
)
from .models import (
    ChartRef,
    HelmRepository,
    HelmOperationResult,
    HelmRelease,
    ReleaseStatus,
)
from .client import PyHelm3Client
from .repositories import DEFAULT_REPOSITORIES
from .resolver import ChartResolver
from .values import HelmValuesValidator

from .accelerator import (
    AcceleratorManager,
    AcceleratorType,
    AcceleratorConfig,
)


LOGGER = logging.getLogger(__name__)


class HelmManager:
    """
    High-level asynchronous Helm integration layer.

    HelmManager exposes SDK-friendly Helm operations while hiding
    the underlying pyhelm3 implementation.
    """

    def __init__(
        self,
        config: HelmConfig,
        repositories: Optional[Sequence[HelmRepository]] = None,
    ) -> None:
        # No default config: HelmConfig requires a kubeconfig, so there is
        # no such thing as a usable default one.
        self._config = config

        LOGGER.info(
            "Initializing HelmManager: executable=%s",
            self._config.executable,
        )

        self._client = PyHelm3Client(self._config)

        # Caller-supplied, because a repository URL is deployment
        # configuration (mirrors, proxies, air-gapped registries), not a
        # fact about the code. Order matters — see repositories.py.
        self._resolver = ChartResolver(
            tuple(repositories) if repositories else DEFAULT_REPOSITORIES,
            timeout=self._config.repository_timeout,
        )

        self._accelerator_manager = AcceleratorManager()

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_release_name(
        release_name: str,
    ) -> None:

        if not release_name:
            raise HelmValidationError(
                "release_name cannot be empty."
            )

        if len(release_name) > 53:
            raise HelmValidationError(
                "release_name cannot exceed 53 characters."
            )

    @staticmethod
    def _validate_chart_name(
        chart_name: str,
    ) -> None:

        if not chart_name:
            raise HelmValidationError(
                "chart_name cannot be empty."
            )

    # ------------------------------------------------------------------
    # Chart resolution
    # ------------------------------------------------------------------
    # Schemes Helm resolves without an index. `oci://` is how Harbor 2.8+
    # serves charts, in the same registry and project as the images --
    # ChartMuseum, which had an index.yaml the resolver could read, is
    # gone from Harbor.
    EXPLICIT_REF_SCHEMES = ("oci://", "http://", "https://")

    @classmethod
    def _is_explicit_ref(cls, chart_name: str) -> bool:
        """Whether `chart_name` is an address rather than a name to search."""
        return chart_name.startswith(cls.EXPLICIT_REF_SCHEMES)

    @staticmethod
    def _local_chart(chart_name: str) -> Optional[Path]:
        """`chart_name` as a local chart, or None if it names a repository chart.

        Only an existing directory holding a Chart.yaml, or an existing
        .tgz, counts. That the path must exist is what keeps "longhorn"
        from being mistaken for a relative directory, and it means a
        typo'd path reports "chart not found in any repository" rather
        than something stranger.
        """
        path = Path(chart_name)
        if path.is_dir() and (path / "Chart.yaml").is_file():
            return path
        if path.is_file() and path.suffix == ".tgz":
            return path
        return None

    async def _chart_source(
        self,
        chart_name: str,
        chart_version: Optional[str],
    ) -> Any:
        """A local path, or a ResolvedChart from the repositories.

        Two kinds of chart reach this layer. Upstream charts are named
        and looked up (`longhorn`, `valkey`); the first-party ones under
        `api/microservices/*/chart` are paths in this repository, and are
        published nowhere for the resolver to find.
        """
        if self._is_explicit_ref(chart_name):
            # Passed through untouched, version included: Helm resolves
            # an oci:// address itself, and for a private registry it
            # uses the credentials `helm registry login` stored. This
            # layer holds none, so a 401 here means nobody has logged in
            # on this machine.
            LOGGER.debug("Using explicit chart ref: ref=%s version=%s",
                         chart_name, chart_version)
            return ChartRef(ref=chart_name, version=chart_version)

        local = self._local_chart(chart_name)
        if local is not None:
            if chart_version:
                raise HelmValidationError(
                    f"chart_version={chart_version!r} was given with the local "
                    f"chart '{chart_name}', which has no versions to choose "
                    "between -- its version is whatever its Chart.yaml says. "
                    "Drop chart_version, or push the chart to a registry "
                    "(oci://...) if you need to pin one."
                )
            LOGGER.debug("Using local chart: path=%s", local)
            return local

        resolved = await self._resolver.resolve(chart_name, chart_version)
        LOGGER.debug(
            "Resolved chart: name=%s repository=%s version=%s",
            resolved.name,
            resolved.repository.name,
            resolved.version,
        )
        return resolved

    async def _fetch_chart(self, chart_source: Any) -> Any:
        """Loads whatever `_chart_source` produced."""
        if isinstance(chart_source, Path):
            return await self._client.get_chart_ref(chart_source)
        if isinstance(chart_source, ChartRef):
            return await self._client.get_chart_ref(
                chart_source.ref, version=chart_source.version
            )
        return await self._client.get_chart(chart_source)

    async def _get_accelerator_chart(
        self,
        *,
        accelerator_config: AcceleratorConfig,
        chart_version: str | None,
    ) -> Any:
        """
        Resolve and fetch the Helm chart for an accelerator.

        Tenstorrent uses a direct OCI chart reference.
        NVIDIA uses the configured Helm repository through ChartResolver.
        """

        if (
            accelerator_config.accelerator_type
            == AcceleratorType.TENSTORRENT
        ):
            if not accelerator_config.chart_reference:
                raise HelmValidationError(
                    "Tenstorrent OCI chart reference "
                    "is not configured."
                )

            LOGGER.debug(
                "Using Tenstorrent OCI chart: "
                "reference=%s version=%s",
                accelerator_config.chart_reference,
                chart_version,
            )

            chart_name = accelerator_config.chart_reference

        elif (
            accelerator_config.accelerator_type
            == AcceleratorType.NVIDIA
        ):
            if not accelerator_config.chart_name:
                raise HelmValidationError(
                    "NVIDIA GPU Operator chart "
                    "is not configured."
                )

            chart_name = accelerator_config.chart_name

        else:
            raise HelmValidationError(
                "Unsupported accelerator type: "
                f"{accelerator_config.accelerator_type.value}"
            )

        chart_source = await self._chart_source(
            chart_name,
            chart_version,
        )
        return await self._fetch_chart(chart_source)

    # ------------------------------------------------------------------
    # Install
    # ------------------------------------------------------------------
    async def install(
        self,
        release_name: str,
        chart_name: str | None = None,
        *,
        accelerator_type: str | AcceleratorType | None = None,
        chart_version: str | None = None,
        namespace: str | None = None,
        values: dict[str, Any] | None = None,
        wait: bool = True,
        atomic: bool = True,
        timeout: str | int | None = None,
        create_namespace: bool = True,
        strict_values: bool = True,
    ) -> HelmOperationResult:
        """
        Install a Helm chart, resolved by name against the repositories
        this manager was given.

        `strict_values` turns unknown value paths into a HelmValidationError.
        It is enabled by default so unsupported user-provided values stop the
        Helm operation before deployment.
        """

        self._validate_release_name(release_name)

        # Create a copy because accelerator preparation may add/default values.
        values = dict(values or {})

        LOGGER.info(
            "Starting Helm install: "
            "release=%s chart=%s accelerator=%s "
            "version=%s namespace=%s",
            release_name,
            chart_name,
            accelerator_type,
            chart_version,
            namespace,
        )

        LOGGER.debug(
            "Helm install values: %s",
            self._redact_values(values),
        )

        # --------------------------------------------------------------
        # Resolve / fetch chart
        # --------------------------------------------------------------
        if accelerator_type is not None:
            if chart_name is not None:
                raise HelmValidationError(
                    "chart_name must not be provided when "
                    "accelerator_type is specified."
                )

            accelerator_config = await self._accelerator_manager.prepare(
                accelerator_type=accelerator_type,
                namespace=namespace,
                values=values,
            )

            chart = await self._get_accelerator_chart(
                accelerator_config=accelerator_config,
                chart_version=chart_version,
            )
        else:
            if chart_name is None:
                raise HelmValidationError(
                    "chart_name is required when "
                    "accelerator_type is not specified."
                )

            self._validate_chart_name(chart_name)

            # Outside the try below, so resolver-specific exceptions stay
            # intact rather than being re-typed as deployment failures.
            chart_source = await self._chart_source(chart_name, chart_version)

            chart = await self._fetch_chart(chart_source)

        # --------------------------------------------------------------
        # Common install flow
        # --------------------------------------------------------------
        try:
            LOGGER.debug(
                "Chart fetched successfully: release=%s", release_name,
            )

            # Retrieve the chart's actual values.yaml through
            # pyhelm3.
            default_values = await chart.values()

            LOGGER.debug(
                "Validating Helm values: "
                "release=%s chart=%s",
                release_name,
                chart.metadata.name,
            )

            # Reports unknown paths rather than refusing them: some
            # charts read values they never declare. See values.py.
            for warning in HelmValuesValidator.validate(
                user_values=values,
                default_values=default_values,
                strict=strict_values,
            ):
                LOGGER.warning("%s", warning)

            LOGGER.debug(
                "Helm values validation completed: "
                "release=%s",
                release_name,
            )

            revision = await self._client.install_or_upgrade_release(
                release_name,
                chart,
                values,
                namespace=namespace,
                wait=wait,
                atomic=atomic,
                create_namespace=create_namespace,
                timeout=timeout,
            )

            result = await self._build_result(
                operation="install",
                revision=revision,
            )

            LOGGER.info(
                "Helm install completed: "
                "release=%s revision=%s status=%s",
                result.release.name,
                result.release.revision,
                result.release.status.value,
            )

            return result

        except Exception as exc:
            self._handle_exception(
                operation="install", release_name=release_name, exc=exc
            )

    # ------------------------------------------------------------------
    # Upgrade
    # ------------------------------------------------------------------
    async def upgrade(
        self,
        release_name: str,
        chart_name: str | None = None,
        *,
        accelerator_type: str | AcceleratorType | None = None,
        chart_version: str | None = None,
        namespace: str | None = None,
        values: dict[str, Any] | None = None,
        wait: bool = True,
        atomic: bool = True,
        timeout: str | int | None = None,
        strict_values: bool = True,
    ) -> HelmOperationResult:
        """
        Upgrade a Helm release using a chart resolved by name against the
        repositories this manager was given.

        `strict_values` turns unknown value paths into a HelmValidationError.
        It is enabled by default so unsupported user-provided values stop the
        Helm operation before deployment.
        """

        self._validate_release_name(release_name)

        # Create a copy because accelerator preparation may add/default values.
        values = dict(values or {})

        LOGGER.info(
            "Starting Helm upgrade: "
            "release=%s chart=%s accelerator=%s "
            "version=%s namespace=%s",
            release_name,
            chart_name,
            accelerator_type,
            chart_version,
            namespace,
        )

        LOGGER.debug(
            "Helm upgrade values: %s",
            self._redact_values(values),
        )

        # --------------------------------------------------------------
        # Resolve / fetch chart
        # --------------------------------------------------------------
        if accelerator_type is not None:
            if chart_name is not None:
                raise HelmValidationError(
                    "chart_name must not be provided when "
                    "accelerator_type is specified."
                )

            accelerator_config = await self._accelerator_manager.prepare(
                accelerator_type=accelerator_type,
                namespace=namespace,
                values=values,
            )

            chart = await self._get_accelerator_chart(
                accelerator_config=accelerator_config,
                chart_version=chart_version,
            )
        else:
            if chart_name is None:
                raise HelmValidationError(
                    "chart_name is required when "
                    "accelerator_type is not specified."
                )

            self._validate_chart_name(chart_name)

            # Outside the try below, so resolver-specific exceptions stay
            # intact rather than being re-typed as deployment failures.
            chart_source = await self._chart_source(chart_name, chart_version)

            chart = await self._fetch_chart(chart_source)

        # --------------------------------------------------------------
        # Common upgrade flow
        # --------------------------------------------------------------
        try:
            LOGGER.debug(
                "Chart fetched successfully: release=%s",
                release_name,
            )

            # Retrieve the chart's actual values.yaml through
            # pyhelm3.
            default_values = await chart.values()

            LOGGER.debug(
                "Validating Helm values: "
                "release=%s chart=%s",
                release_name,
                chart.metadata.name,
            )

            # Reports unknown paths rather than refusing them: some
            # charts read values they never declare. See values.py.
            for warning in HelmValuesValidator.validate(
                user_values=values,
                default_values=default_values,
                strict=strict_values,
            ):
                LOGGER.warning("%s", warning)

            LOGGER.debug(
                "Helm values validation completed: "
                "release=%s",
                release_name,
            )

            revision = await self._client.install_or_upgrade_release(
                release_name,
                chart,
                values,
                namespace=namespace,
                wait=wait,
                atomic=atomic,
                create_namespace=False,
                timeout=timeout,
            )

            result = await self._build_result(
                operation="upgrade",
                revision=revision,
            )

            LOGGER.info(
                "Helm upgrade completed: "
                "release=%s revision=%s status=%s",
                result.release.name,
                result.release.revision,
                result.release.status.value,
            )

            return result

        except Exception as exc:
            self._handle_exception(
                operation="upgrade", release_name=release_name, exc=exc
            )

    # ------------------------------------------------------------------
    # Uninstall
    # ------------------------------------------------------------------
    async def uninstall(
        self,
        release_name: str,
        *,
        namespace: str | None = None,
        wait: bool = True,
        timeout: str | int | None = None,
        keep_history: bool = False,
        missing_ok: bool = False,
    ) -> HelmOperationResult:
        """Uninstalls a release.

        Raises `HelmReleaseNotFoundError` when there is no such release,
        unless `missing_ok`. An earlier version returned a result saying
        status "uninstalled" in that case, so a caller could not tell "I
        removed it" from "it was never there" — and `helm uninstall` itself
        exits 1.
        """
        self._validate_release_name(release_name)

        LOGGER.info(
            "Starting Helm uninstall: "
            "release=%s namespace=%s",
            release_name,
            namespace,
        )

        # pyhelm3 does not surface a missing release as an error, so ask
        # first rather than reporting a removal that never happened.
        if not missing_ok:
            try:
                await self._client.get_current_revision(
                    release_name, namespace=namespace
                )
            except Exception as exc:
                self._handle_exception(
                    operation="uninstall", release_name=release_name, exc=exc
                )

        try:
            await self._client.uninstall_release(
                release_name,
                namespace=namespace,
                wait=wait,
                timeout=timeout,
                keep_history=keep_history,
            )

            LOGGER.info(
                "Helm uninstall completed: release=%s",
                release_name,
            )

        except Exception as exc:
            self._handle_exception(
                operation="uninstall", release_name=release_name, exc=exc
            )

        status = ReleaseStatus.UNINSTALLED

        result = HelmOperationResult(
            operation="uninstall",
            release=HelmRelease(
                name=release_name,
                namespace=namespace,
                revision=None,
                status=status,
            ),
        )

        return result

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    async def status(
        self,
        release_name: str,
        *,
        namespace: str | None = None,
    ) -> HelmRelease:

        self._validate_release_name(release_name)

        LOGGER.debug(
            "Getting Helm release status: "
            "release=%s namespace=%s",
            release_name,
            namespace,
        )

        try:
            revision = await self._client.get_current_revision(
                release_name,
                namespace=namespace,
            )

            return await self._build_release(
                revision
            )

        except Exception as exc:
            self._handle_exception(
                operation="status", release_name=release_name, exc=exc
            )

    # ------------------------------------------------------------------
    # Get Release
    # ------------------------------------------------------------------
    async def get_release(
        self,
        release_name: str,
        *,
        namespace: str | None = None,
    ) -> Optional[HelmRelease]:
        """The release as it currently stands, or None when there is none.

        Two questions, two methods. `status()` raises for a missing
        release, which is right when the caller expected one to be there.
        This is for the other question — *is* there one — where absence is
        an ordinary answer and not worth an exception.

        Taken from the helm-manager-sdk branch (89869a7), where it scanned
        `list_releases()` for a name match. This looks the release up
        directly instead: one `helm status` rather than a listing plus a
        revision fetch per candidate, and it is the pattern pyhelm3 itself
        uses internally.
        """
        try:
            return await self.status(release_name, namespace=namespace)
        except HelmReleaseNotFoundError:
            return None

    # ------------------------------------------------------------------
    # Result conversion
    # ------------------------------------------------------------------
    @classmethod
    async def _build_result(
        cls,
        operation: str,
        revision: Any,
    ) -> HelmOperationResult:

        return HelmOperationResult(
            operation=operation,
            release=await cls._build_release(
                revision
            ),
        )

    @staticmethod
    async def _build_release(
        revision: Any,
    ) -> HelmRelease:

        status_value = str(
            revision.status.value
        ).lower()

        try:
            status = ReleaseStatus(status_value)
        except ValueError:
            status = ReleaseStatus.UNKNOWN

        # chart_name and chart_version were declared on HelmRelease and
        # never filled in, so `status()` always reported chart=None --
        # which is the field you want when checking what version is
        # actually deployed.
        #
        # An install or upgrade has the metadata embedded in its status
        # already, so reading it there is free. A `helm status` does not
        # (measured: chart_metadata_ is None), so this costs one extra
        # helm invocation per status call. Worth it at the scale these
        # run: provisioning, not a request path.
        chart_name = None
        chart_version = None
        try:
            metadata = await revision.chart_metadata()
        except Exception as exc:      # noqa: BLE001 - supplementary only
            # Deliberately not fatal, and deliberately not silent. The
            # release's name, revision and status are the answer the
            # caller asked for; failing all of it because a follow-up
            # lookup failed would be the wrong trade.
            LOGGER.debug(
                "chart metadata unavailable for release=%s revision=%s: %s",
                revision.release.name, revision.revision, exc,
            )
        else:
            chart_name = metadata.name
            chart_version = str(metadata.version)

        return HelmRelease(
            name=revision.release.name,
            namespace=revision.release.namespace,
            revision=revision.revision,
            status=status,
            chart_name=chart_name,
            chart_version=chart_version,
        )

    # ------------------------------------------------------------------
    # Exception handling
    # ------------------------------------------------------------------
    @staticmethod
    def _handle_exception(
        *,
        operation: str,
        release_name: str,
        exc: Exception,
    ) -> NoReturn:
        """Re-raises `exc` as this layer's own error type.

        Annotated NoReturn so callers don't need an unreachable `raise`
        after calling it to convince a type checker.

        Matching is on the real pyhelm3 classes, imported here. An earlier
        version compared `type(exc).__name__` against string literals,
        which breaks silently if pyhelm3 renames anything and also matched
        Python's builtin TimeoutError — so an unrelated socket timeout
        became a HelmTimeoutError.
        """
        try:
            from pyhelm3 import CommandCancelledError, ReleaseNotFoundError
        except ImportError:      # pragma: no cover - the extra is missing
            # isinstance(exc, ()) is always False, so each check below
            # falls through to the generic error when the extra is absent.
            CommandCancelledError = ReleaseNotFoundError = ()

        # "There is no such release" is an answer, not a failure — and it
        # is the answer on the ordinary first-install path, since
        # is_deployed() asks before every install. Logging it at exception
        # level filled the log with tracebacks for the expected case,
        # which is the noise that hides a real one. Debug, and no
        # traceback. Spotted on the helm-manager-sdk branch (89869a7).
        if isinstance(exc, ReleaseNotFoundError):
            LOGGER.debug(
                "Helm release not found: operation=%s release=%s",
                operation,
                release_name,
            )
            raise HelmReleaseNotFoundError(
                f"Helm release '{release_name}' was not found."
            ) from exc

        LOGGER.exception(
            "Helm operation failed: operation=%s release=%s",
            operation,
            release_name,
        )

        if isinstance(exc, CommandCancelledError):
            raise HelmOperationCancelledError(
                f"Helm {operation} operation was cancelled."
            ) from exc

        if isinstance(exc, asyncio.TimeoutError):
            raise HelmTimeoutError(
                f"Helm {operation} operation timed out."
            ) from exc

        raise HelmDeploymentError(
            f"Helm {operation} failed for release '{release_name}': {exc}"
        ) from exc

    # ------------------------------------------------------------------
    # Logging helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _redact_values(
        values: dict[str, Any],
    ) -> dict[str, Any]:
        """Delegates to `multistack.helm.redact()`.

        This used to carry its own copy of the sensitive-key set, missing
        `secretKey`/`accessKey` -- the exact keys a MinIO tenant's values
        use -- while the module-level `redact()` already had them right
        and was already tested for it (`test_credentials_are_redacted`).
        One set now, not two silently drifting apart.

        Imported here rather than at module load: `multistack/helm/__init__.py`
        already imports `manager.HelmManager` lazily, inside a method, for
        the same reason -- this is the same pattern in the other direction.
        """
        from . import redact

        return redact(values)