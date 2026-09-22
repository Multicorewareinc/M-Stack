from __future__ import annotations

import json
from typing import List, Optional

from pydantic import BaseModel, ConfigDict

from .client import NatsAdminClient
from .config import NatsDeployment


class ConsumerInfo(BaseModel):
    """A durable JetStream consumer's current state, read-only.

    `num_ack_pending == 0` with `delivered_*` caught up to the stream's
    own `last_seq` is the signal that matters: a stuck `num_pending`
    means a consumer bound but isn't
    acking, and one missing from `NatsConsumerInspector.list` entirely
    means it never bound at all.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    stream: str
    filter_subject: Optional[str] = None
    num_pending: int
    num_ack_pending: int
    num_redelivered: int
    delivered_stream_seq: int
    delivered_consumer_seq: int


class NatsConsumerInspector:
    """Read-only visibility into the durable *push* consumers this
    platform's services (rate-limiter-rpm, enricher, rate-limiter-tpm,
    billing) bind at startup -- this class never creates or binds one
    itself, and that is a deliberate omission, not a gap.

    A durable push consumer needs a `deliver_subject` the client
    generates itself (an ephemeral inbox) at bind time; a consumer
    created from outside the process that will actually consume it would
    either sit unused (the real client can't discover its own inbox from
    here) or actively conflict with it -- "consumer is already bound to a
    subscription", a failure this package has hit in production. So
    this answers "is it bound and
    keeping up", which is the operational half of ADR-030 a deployment
    tool can actually own, not "create one".
    """

    def __init__(self, admin_client: Optional[NatsAdminClient] = None) -> None:
        self._admin_client = admin_client or NatsAdminClient()

    def list(self, deployment: NatsDeployment, stream: str) -> List[str]:
        self._admin_client.ensure(deployment)
        output = self._admin_client.exec(deployment, "consumer", "ls", stream, "-j")
        return json.loads(output) if output.strip() else []

    def info(self, deployment: NatsDeployment, stream: str, consumer: str) -> ConsumerInfo:
        self._admin_client.ensure(deployment)
        output = self._admin_client.exec(deployment, "consumer", "info", stream, consumer, "-j")
        payload = json.loads(output)
        delivered = payload.get("delivered", {})
        return ConsumerInfo(
            name=consumer,
            stream=stream,
            filter_subject=payload.get("config", {}).get("filter_subject"),
            num_pending=payload.get("num_pending", 0),
            num_ack_pending=payload.get("num_ack_pending", 0),
            num_redelivered=payload.get("num_redelivered", 0),
            delivered_stream_seq=delivered.get("stream_seq", 0),
            delivered_consumer_seq=delivered.get("consumer_seq", 0),
        )
