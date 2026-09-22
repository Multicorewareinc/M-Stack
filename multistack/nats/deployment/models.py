from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class NatsDeploymentInfo(BaseModel):
    """Information about a deployed NATS instance."""

    model_config = ConfigDict(
        extra="forbid",
    )

    release_name: str
    namespace: str
    status: str

    client_url: str
    monitor_url: str
    metrics_url: str