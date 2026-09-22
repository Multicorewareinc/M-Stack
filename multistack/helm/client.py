from __future__ import annotations

from typing import Any

from .config import HelmConfig
from .models import ResolvedChart


class PyHelm3Client:
    """
    Adapter around pyhelm3.Client.

    HelmManager interacts with this class instead of directly interacting
    with pyhelm3.
    """

    def __init__(
        self,
        config: HelmConfig,
    ) -> None:
        self._config = config

        # Imported here rather than at module scope so `import
        # multistack.helm` stays free for anyone who installed the SDK
        # without the `helm` extra. Same reason the driver registry names
        # modules instead of importing them.
        from pyhelm3 import Client

        self._client = Client(
            executable=config.executable,
            kubeconfig=config.kubeconfig,
            kubecontext=config.kubecontext,
            default_timeout=config.default_timeout,
            history_max_revisions=config.history_max_revisions,
            insecure_skip_tls_verify=config.insecure_skip_tls_verify,
        )

    async def get_current_revision(
        self,
        release_name: str,
        *,
        namespace: str | None = None,
    ) -> Any:
        """The release's current revision.

        HelmManager.status() called this and the adapter did not define
        it, so status() raised AttributeError, which the blanket handler
        then reported as a Helm deployment failure. pyhelm3.Client has the
        method; only the forwarding was missing.
        """
        return await self._client.get_current_revision(
            release_name, namespace=namespace
        )

    async def get_chart_ref(
        self,
        ref: Any,
        version: Any = None,
    ) -> Any:
        """A chart from a reference Helm can resolve on its own.

        A local directory, a packaged `.tgz`, or an `oci://` address.
        `repo=None` is what makes Helm treat the argument as a reference
        rather than a chart name to look up in a repository -- the same
        thing `helm install ./chart` and `helm install oci://...` do.

        Both cases exist because the resolver cannot help with either.
        The first-party charts under `api/microservices/*/chart` are
        published nowhere, and an OCI registry has no index.yaml to
        search.
        """
        return await self._client.get_chart(ref, version=version)

    async def get_chart(
        self,
        resolved_chart: ResolvedChart,
    ) -> Any:
        """
        Fetch a Helm chart from the resolved repository.
        """

        return await self._client.get_chart(
            resolved_chart.name,
            repo=resolved_chart.repository.url,
            version=resolved_chart.version,
        )

    async def get_oci_chart(
        self,
        chart_reference: str,
        *,
        version: str | None = None,
    ) -> Any:
        """
        Fetch a Helm chart directly from an OCI registry.
        """

        return await self._client.get_chart(
            chart_reference,
            version=version,
        )

    async def install_or_upgrade_release(
        self,
        release_name: str,
        chart: Any,
        values: dict[str, Any],
        *,
        namespace: str | None = None,
        wait: bool = True,
        atomic: bool = True,
        create_namespace: bool = False,
        timeout: str | int | None = None,
    ) -> Any:
        """
        Install or upgrade a Helm release through pyhelm3.
        """

        return await self._client.install_or_upgrade_release(
            release_name,
            chart,
            values,
            namespace=namespace,
            wait=wait,
            atomic=atomic,
            create_namespace=create_namespace,
            timeout=timeout,
        )

    async def uninstall_release(
        self,
        release_name: str,
        *,
        namespace: str | None = None,
        wait: bool = True,
        timeout: str | int | None = None,
        keep_history: bool = False,
    ) -> Any:
        """
        Uninstall a Helm release through pyhelm3.
        """

        return await self._client.uninstall_release(
            release_name,
            namespace=namespace,
            wait=wait,
            timeout=timeout,
            keep_history=keep_history,
        )

    async def list_releases(
        self,
        *,
        namespace: str | None = None,
    ) -> list[Any]:
        return await self._client.list_releases(
            all=True,
            namespace=namespace,
        )