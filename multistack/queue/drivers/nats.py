"""The queue capability's JetStream implementation.

Wraps `multistack.nats.deployment` rather than reimplementing it: that
package already has the Helm mechanics (`NatsDeploymentManager`), the
manual-admin client (`NatsAdminClient`) and idempotent stream creation
(`NatsStreamManager`). This driver is the
`CapabilitySpec`-conformant front door onto it.

`create()`/`update()` also call `ensure_streams()` after the Helm release
is up -- the two JetStream streams (`GATEWAY_EVENTS`,
`GATEWAY_EVENTS_ENRICHED`) are part of what "the event backbone exists"
means, the same way `examples/full_stack.py`'s old hand-rolled `nats`
stage used to do by hand before this capability existed.
"""
from typing import List

from ...kube import require_cli, require_cluster
from ...nats.deployment import (
    ConsumerInfo,
    NatsDeployment,
    NatsDeploymentManager,
)
from ..base import QueueError, QueuePrerequisiteError
from ..spec import Queue


class JetStreamQueueDriver:
    def __init__(self) -> None:
        self._manager = NatsDeploymentManager()

    def _deployment(self, queue: Queue) -> NatsDeployment:
        options = queue.options
        return NatsDeployment(
            kubeconfig_path=queue.kubeconfig_path,
            release_name=options.release_name,
            namespace=queue.resolved_namespace,
            chart=options.chart,
            chart_version=options.chart_version,
            replicas=options.replicas,
            storage_class=queue.storage_class or "longhorn",
            pvc_size=options.pvc_size,
            file_store_max_size=options.file_store_max_size,
            memory_store_max_size=options.memory_store_max_size,
            image_tag=options.image_tag,
            extra_values=options.extra_values,
        )

    def check_prerequisites(self, queue: Queue) -> List[str]:
        """Verifies the cluster can host this release.

        Not `require_storage_class`: `CapabilityBackend.verify_requirements`
        already runs it generically for any spec whose `REQUIRES` names
        "storage", before this is ever called (see
        `multistack/capability.py`) -- duplicating it here would just run
        the same kubectl call twice.
        """
        require_cli(
            "helm", error_cls=QueuePrerequisiteError,
            purpose="the queue driver drives it directly",
        )
        require_cluster(queue.kubeconfig_path, capability="queue")
        return []

    def create(self, queue: Queue) -> str:
        deployment = self._deployment(queue)
        try:
            info = self._manager.create(deployment)
            self._manager.ensure_streams(deployment)
        except Exception as exc:
            raise QueueError(str(exc)) from exc
        return info.client_url

    def update(self, queue: Queue) -> str:
        """Helm upgrade/install semantics, same as `NatsDeploymentManager
        .update`, so update and create share the same path."""
        return self.create(queue)

    def delete(self, queue: Queue) -> None:
        deployment = self._deployment(queue)
        try:
            self._manager.delete(deployment)
        except Exception as exc:
            raise QueueError(str(exc)) from exc

    def exists(self, queue: Queue) -> bool:
        return self._manager.is_deployed(self._deployment(queue))

    def list_streams(self, queue: Queue) -> List[str]:
        return self._manager.streams.list(self._deployment(queue))

    def list_consumers(self, queue: Queue, stream: str) -> List[str]:
        return self._manager.list_consumers(self._deployment(queue), stream)

    def consumer_info(
        self, queue: Queue, stream: str, consumer: str,
    ) -> ConsumerInfo:
        return self._manager.consumer_info(
            self._deployment(queue), stream, consumer,
        )
