from __future__ import annotations

from multistack.kube import apply, kubectl

from .config import NatsDeployment
from .errors import NatsDeploymentError, NatsTimeoutError


class NatsAdminClient:
    """The one manual-administration NATS client this platform runs --
    `natsio/nats-box`, holding the `nats` CLI, since no `nats` binary
    exists off-cluster. Every stream/consumer operation in
    this package goes through it via `kubectl exec` -- the SDK itself
    never opens a direct NATS connection, the same way every other
    capability here reaches the cluster through kubectl/helm alone.
    """

    pod_name = "nats-box"
    image = "natsio/nats-box:latest"

    def ensure(self, deployment: NatsDeployment, *, timeout: int = 90) -> None:
        """Deploys the admin pod if it isn't already running. Idempotent --
        a second call against an existing pod is a no-op, so this is safe
        to call before every stream/consumer operation rather than once
        up front."""
        if self._is_running(deployment):
            return

        apply(
            deployment.kubeconfig_path,
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {
                    "name": self.pod_name,
                    "namespace": deployment.namespace,
                    "labels": {"app": self.pod_name},
                },
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": self.pod_name,
                            "image": self.image,
                            # A long sleep, not a one-shot: this pod is left
                            # running for repeated `kubectl exec` calls
                            # rather than spun up per command.
                            "command": ["sleep", "86400"],
                        }
                    ],
                },
            },
            error_cls=NatsDeploymentError,
        )

        try:
            kubectl(
                deployment.kubeconfig_path,
                "wait", "--for=condition=Ready",
                f"pod/{self.pod_name}", "-n", deployment.namespace,
                f"--timeout={timeout}s",
                error_cls=NatsTimeoutError,
            )
        except NatsDeploymentError as exc:
            raise NatsTimeoutError(
                f"{self.pod_name} did not become Ready within {timeout}s: {exc}"
            ) from exc

    def exec(
        self,
        deployment: NatsDeployment,
        *args: str,
        check: bool = True,
    ) -> str:
        """Runs `nats <args> --server <client_url>` inside the admin pod.

        `check=False` returns whatever the command printed even on a
        non-zero exit, rather than raising -- existence probes (does this
        stream already exist?) want the output, not an exception, on the
        "no" answer.
        """
        return kubectl(
            deployment.kubeconfig_path,
            "exec", "-n", deployment.namespace, self.pod_name, "--",
            "nats", *args, "--server", self.server_url(deployment),
            check=check,
            error_cls=NatsDeploymentError,
        )

    def delete(self, deployment: NatsDeployment) -> None:
        """Removes the admin pod. Never required for stream/consumer
        operations to keep working -- only for tearing the client down
        deliberately."""
        kubectl(
            deployment.kubeconfig_path,
            "delete", "pod", self.pod_name, "-n", deployment.namespace,
            "--ignore-not-found",
            check=False,
            error_cls=NatsDeploymentError,
        )

    def _is_running(self, deployment: NatsDeployment) -> bool:
        phase = kubectl(
            deployment.kubeconfig_path,
            "get", "pod", self.pod_name, "-n", deployment.namespace,
            "-o", "jsonpath={.status.phase}",
            check=False,
            error_cls=NatsDeploymentError,
        ).strip()
        return phase == "Running"

    @staticmethod
    def server_url(deployment: NatsDeployment) -> str:
        return (
            f"nats://{deployment.release_name}."
            f"{deployment.namespace}.svc.cluster.local:4222"
        )
