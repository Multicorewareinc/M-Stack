from __future__ import annotations

from typing import Any, ClassVar, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class NatsDeployment(BaseModel):
    """
    Declarative definition of a NATS deployment.

    NATS is deployed into an existing Kubernetes/RKE2 cluster using
    the official NATS Helm chart.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
    )

    kubeconfig_path: str

    release_name: str = "nats"
    namespace: str = "nats"

    chart: str = "nats"
    chart_version: Optional[str] = None

    # NATS cluster configuration.
    replicas: int = 3

    # JetStream configuration.
    jetstream_enabled: bool = True

    storage_class: str = "longhorn"
    pvc_size: str = "20Gi"
    file_store_max_size: str = "15Gi"
    memory_store_max_size: str = "2Gi"

    # NATS server image.
    image_repository: str = "nats"
    image_tag: str = "2.14.6-alpine"

    # NATS server resources.
    cpu_request: str = "500m"
    memory_request: str = "1Gi"
    cpu_limit: str = "2"
    memory_limit: str = "4Gi"

    go_memory_limit: str = "3GiB"

    # NATS server configuration.
    max_payload: int = 8388608
    write_deadline: str = "10s"

    # Prometheus exporter.
    exporter_enabled: bool = True
    exporter_image_repository: str = "natsio/prometheus-nats-exporter"
    exporter_image_tag: str = "0.20.1"

    # Pod disruption budget.
    pod_disruption_budget_enabled: bool = False

    # Additional Helm values supplied by the caller.
    extra_values: Dict[str, Any] = Field(default_factory=dict)

    REQUIRES: ClassVar[tuple] = (
        "cluster",
        "storage",
    )

    @model_validator(mode="after")
    def _validate_on_construction(self):
        self.validate()
        return self

    def validate(self) -> None:
        if not self.kubeconfig_path:
            raise ValueError(
                "kubeconfig_path is required because NATS is deployed "
                "into an existing Kubernetes cluster."
            )

        if not self.release_name:
            raise ValueError("release_name is required")

        if not self.namespace:
            raise ValueError("namespace is required")

        if not self.chart:
            raise ValueError("chart is required")

        if self.replicas < 1:
            raise ValueError(
                f"replicas must be at least 1, got {self.replicas}"
            )

        if self.jetstream_enabled and self.replicas < 2:
            raise ValueError(
                "replicas must be at least 2 when JetStream is enabled"
            )

        if self.jetstream_enabled and not self.storage_class:
            raise ValueError(
                "storage_class is required when persistent "
                "JetStream storage is enabled"
            )

        if self.max_payload <= 0:
            raise ValueError(
                "max_payload must be greater than 0"
            )

    def helm_values(self) -> Dict[str, Any]:
        """
        Build values for the official NATS Helm chart.
        """

        values: Dict[str, Any] = {
            "config": {
                "cluster": {
                    "enabled": True,
                    "replicas": self.replicas,
                },

                "jetstream": {
                    "enabled": self.jetstream_enabled,

                    "fileStore": {
                        "enabled": self.jetstream_enabled,
                        "pvc": {
                            "enabled": self.jetstream_enabled,
                            "size": self.pvc_size,
                            "storageClassName": self.storage_class,
                        },
                        "maxSize": self.file_store_max_size,
                    },

                    "memoryStore": {
                        "enabled": self.jetstream_enabled,
                        "maxSize": self.memory_store_max_size,
                    },
                },

                # Client connection port.
                "nats": {
                    "port": 4222,
                },

                # Monitoring endpoint used by health checks and
                # prometheus-nats-exporter.
                "monitor": {
                    "enabled": True,
                    "port": 8222,
                },

                # Additional NATS server configuration.
                "merge": {
                    "max_payload": self.max_payload,
                    "write_deadline": self.write_deadline,
                },
            },

            "container": {
                "image": {
                    "repository": self.image_repository,
                    "tag": self.image_tag,
                },

                "resources": {
                    "requests": {
                        "cpu": self.cpu_request,
                        "memory": self.memory_request,
                    },
                    "limits": {
                        "cpu": self.cpu_limit,
                        "memory": self.memory_limit,
                    },
                },

                "env": {
                    "GOMEMLIMIT": self.go_memory_limit,
                },
            },

            "promExporter": {
                "enabled": self.exporter_enabled,
                "image": {
                    "repository": self.exporter_image_repository,
                    "tag": self.exporter_image_tag,
                },
                "port": 7777,
            },

            "service": {
                "enabled": True,
                "ports": {
                    "nats": {
                        "enabled": True,
                    },
                    "cluster": {
                        "enabled": True,
                    },
                    "monitor": {
                        "enabled": True,
                    },
                },
                "name": "nats",
            },

            "podDisruptionBudget": {
                "enabled": self.pod_disruption_budget_enabled,
            },
        }

        _deep_merge(values, self.extra_values)

        return values


def _deep_merge(
    destination: Dict[str, Any],
    source: Dict[str, Any],
) -> None:
    """
    Recursively merge source into destination.

    Values in source take precedence.
    """

    for key, value in source.items():
        if (
            key in destination
            and isinstance(destination[key], dict)
            and isinstance(value, dict)
        ):
            _deep_merge(destination[key], value)
        else:
            destination[key] = value