from __future__ import annotations

import json

from multistack.kube import require_cli, require_cluster, run_local

from .config import NatsDeployment
from .errors import (
    NatsDeploymentError,
    NatsPrerequisiteError,
)


class NatsPrerequisiteManager:
    """Validates prerequisites required to deploy NATS."""

    def check(self, deployment: NatsDeployment) -> None:
        deployment.validate()

        require_cli(
            "helm",
            error_cls=NatsPrerequisiteError,
            purpose="NATS deployment uses Helm",
        )

        require_cli(
            "kubectl",
            error_cls=NatsPrerequisiteError,
            purpose="NATS deployment validates Kubernetes resources",
        )

        require_cluster(
            deployment.kubeconfig_path,
            capability="NATS",
        )

        if deployment.jetstream_enabled:
            self._require_storage_class(deployment)

    def _require_storage_class(
        self,
        deployment: NatsDeployment,
    ) -> None:
        output = run_local(
            [
                "kubectl",
                "--kubeconfig",
                deployment.kubeconfig_path,
                "get",
                "storageclass",
                "-o",
                "json",
            ],
            error_cls=NatsDeploymentError,
        )

        try:
            payload = json.loads(output)
        except json.JSONDecodeError as exc:
            raise NatsPrerequisiteError(
                "Unable to parse StorageClass information returned "
                "by Kubernetes."
            ) from exc

        available = {
            item["metadata"]["name"]
            for item in payload.get("items", [])
            if item.get("metadata", {}).get("name")
        }

        if deployment.storage_class not in available:
            found = ", ".join(sorted(available)) or "none"

            raise NatsPrerequisiteError(
                f"StorageClass '{deployment.storage_class}' does not "
                f"exist on the cluster (found: {found}). "
                "JetStream PVCs would remain Pending."
            )