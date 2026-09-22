from __future__ import annotations

from typing import List, Optional

from multistack.helm import (
    HelmRelease,
    HelmRunner,
    ReleaseStatus,
)

from .client import NatsAdminClient
from .config import NatsDeployment
from .consumers import ConsumerInfo, NatsConsumerInspector
from .errors import NatsDeploymentError
from .models import NatsDeploymentInfo
from .prerequisites import NatsPrerequisiteManager
from .streams import DEFAULT_STREAMS, NatsStreamManager, StreamConfig


class NatsDeploymentManager:
    """
    Deploys and manages NATS on an existing Kubernetes cluster.

    Helm mechanics are delegated to HelmRunner. Stream creation and
    consumer inspection (`ensure_streams`/`list_consumers`/
    `consumer_info`) go through the admin client pod's `nats` CLI instead
    -- see `NatsAdminClient`, `NatsStreamManager`, `NatsConsumerInspector`.
    """

    log_prefix = "nats"

    def __init__(self) -> None:
        self._prerequisites = NatsPrerequisiteManager()
        self.admin_client = NatsAdminClient()
        self.streams = NatsStreamManager(self.admin_client)
        self.consumers = NatsConsumerInspector(self.admin_client)

    def create(
        self,
        deployment: NatsDeployment,
    ) -> NatsDeploymentInfo:
        """
        Install or upgrade NATS.

        The operation is idempotent: an existing healthy release is
        upgraded in place.
        """

        self._prerequisites.check(deployment)

        runner = self._helm_runner(deployment)

        existing = runner.get_release(
            deployment.release_name,
            namespace=deployment.namespace,
        )

        if (
            existing is not None
            and existing.status is ReleaseStatus.FAILED
        ):
            print(
                f"[{self.log_prefix}] clearing failed release "
                f"{deployment.release_name}"
            )

            runner.uninstall(
                deployment.release_name,
                namespace=deployment.namespace,
            )

        print(
            f"[{self.log_prefix}] deploying "
            f"{deployment.release_name} "
            f"({deployment.replicas} replicas)"
        )

        release = runner.install_or_upgrade(
            deployment.release_name,
            chart=deployment.chart,
            chart_version=deployment.chart_version,
            namespace=deployment.namespace,
            values=deployment.helm_values(),
            atomic=True,
            wait=True,
            create_namespace=True,
            strict_values=True,
        )

        info = self._deployment_info(
            deployment,
            release,
        )

        print(
            f"[{self.log_prefix}] release "
            f"{deployment.release_name} "
            f"{info.status}"
        )

        return info

    def ensure_streams(
        self,
        deployment: NatsDeployment,
        streams: Optional[List[StreamConfig]] = None,
    ) -> None:
        """Deploys the admin client pod (if absent) and creates the
        platform's JetStream streams if they don't already exist.
        Idempotent -- safe to call on every deploy, not just the
        first one."""

        self.streams.ensure(deployment, streams if streams is not None else DEFAULT_STREAMS)

    def list_consumers(self, deployment: NatsDeployment, stream: str) -> List[str]:
        """Durable consumer names currently bound on `stream` -- read-only,
        see `NatsConsumerInspector` for why this package never creates
        one itself."""

        return self.consumers.list(deployment, stream)

    def consumer_info(
        self,
        deployment: NatsDeployment,
        stream: str,
        consumer: str,
    ) -> ConsumerInfo:
        """Current delivery/ack state for one durable consumer -- the
        same check this package would otherwise require doing by
        hand."""

        return self.consumers.info(deployment, stream, consumer)

    def update(
        self,
        deployment: NatsDeployment,
    ) -> NatsDeploymentInfo:
        """
        Update the NATS deployment.

        HelmRunner uses upgrade/install semantics, therefore update and
        create share the same deployment path.
        """

        return self.create(deployment)

    def delete(
        self,
        deployment: NatsDeployment,
    ) -> None:
        """Remove the NATS Helm release."""

        deployment.validate()

        runner = self._helm_runner(deployment)

        runner.uninstall(
            deployment.release_name,
            namespace=deployment.namespace,
            wait=True,
            missing_ok=True,
        )

        print(
            f"[{self.log_prefix}] uninstalled release "
            f"{deployment.release_name}"
        )

    def status(
        self,
        deployment: NatsDeployment,
    ) -> Optional[NatsDeploymentInfo]:
        """Return current NATS deployment information."""

        deployment.validate()

        release = self._helm_runner(
            deployment
        ).get_release(
            deployment.release_name,
            namespace=deployment.namespace,
        )

        if release is None:
            return None

        return self._deployment_info(
            deployment,
            release,
        )

    def is_deployed(
        self,
        deployment: NatsDeployment,
    ) -> bool:
        """Return whether NATS is successfully deployed."""

        deployment.validate()

        return self._helm_runner(
            deployment
        ).is_deployed(
            deployment.release_name,
            namespace=deployment.namespace,
        )

    def _helm_runner(
        self,
        deployment: NatsDeployment,
    ) -> HelmRunner:
        return HelmRunner(
            deployment.kubeconfig_path,
            error_cls=NatsDeploymentError,
        )

    @staticmethod
    def _deployment_info(
        deployment: NatsDeployment,
        release: HelmRelease,
    ) -> NatsDeploymentInfo:
        status = (
            release.status.value
            if hasattr(release.status, "value")
            else str(release.status)
        )

        service = deployment.release_name

        return NatsDeploymentInfo(
            release_name=deployment.release_name,
            namespace=deployment.namespace,
            status=status,

            client_url=(
                f"nats://{service}."
                f"{deployment.namespace}.svc.cluster.local:4222"
            ),

            monitor_url=(
                f"http://{service}."
                f"{deployment.namespace}.svc.cluster.local:8222"
            ),

            metrics_url=(
                f"http://{service}."
                f"{deployment.namespace}.svc.cluster.local:7777"
            ),
        )