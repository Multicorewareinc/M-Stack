from __future__ import annotations

import json
from typing import List, Optional

from pydantic import BaseModel, ConfigDict

from .client import NatsAdminClient
from .config import NatsDeployment
from .errors import NatsDeploymentError


class StreamConfig(BaseModel):
    """One JetStream stream this platform's event backbone needs."""

    model_config = ConfigDict(extra="forbid")

    name: str
    subject: str
    storage: str = "file"
    retention: str = "limits"
    max_age: str = "24h"


# The two streams ADR-030's raw/enriched split needs: the
# gateway's own `gateway.events`, and the enricher's derived
# `gateway.events.enriched` that rate-limiter-tpm and billing both read
# instead of calling the tokenizer themselves. Retention differs on
# purpose -- the enriched stream is what billing bills off of, so it
# keeps a longer window than the raw input it was built from.
DEFAULT_STREAMS: List[StreamConfig] = [
    StreamConfig(name="GATEWAY_EVENTS", subject="gateway.events", max_age="24h"),
    StreamConfig(
        name="GATEWAY_EVENTS_ENRICHED", subject="gateway.events.enriched", max_age="72h"
    ),
]


class NatsStreamManager:
    """Creates the platform's JetStream streams through the admin client's
    `nats` CLI (see `NatsAdminClient`).

    Idempotent, and deliberately so beyond just "safe to call twice": each
    service's own consumer also calls `add_stream` at startup (a no-op if
    a matching stream already exists) -- what this buys is a stream
    existing *before* the first service starts, rather than racing an
    implicit creation with whichever pod happens to bind first.
    """

    def __init__(self, admin_client: Optional[NatsAdminClient] = None) -> None:
        self._admin_client = admin_client or NatsAdminClient()

    def ensure(
        self,
        deployment: NatsDeployment,
        streams: Optional[List[StreamConfig]] = None,
    ) -> None:
        self._admin_client.ensure(deployment)
        for stream in streams if streams is not None else DEFAULT_STREAMS:
            if self._exists(deployment, stream.name):
                print(f"[nats] stream {stream.name} already exists")
                continue
            self._create(deployment, stream)

    def list(self, deployment: NatsDeployment) -> List[str]:
        self._admin_client.ensure(deployment)
        output = self._admin_client.exec(deployment, "stream", "ls", "-j")
        if not output.strip():
            return []
        # `nats stream ls -j` prints the bare JSON literal "null" (not
        # "[]") when there are zero streams -- a from-empty first deploy
        # hits this every time, since that's exactly when this list is
        # shortest.
        parsed = json.loads(output)
        return parsed if parsed is not None else []

    def _exists(self, deployment: NatsDeployment, name: str) -> bool:
        return name in self.list(deployment)

    def _create(self, deployment: NatsDeployment, stream: StreamConfig) -> None:
        print(f"[nats] creating stream {stream.name} (subject={stream.subject})")
        try:
            self._admin_client.exec(
                deployment,
                "stream", "add", stream.name,
                f"--subjects={stream.subject}",
                f"--storage={stream.storage}",
                f"--retention={stream.retention}",
                f"--max-age={stream.max_age}",
                "--max-msgs=-1", "--max-bytes=-1", "--max-msg-size=-1",
                "--defaults",
            )
        except NatsDeploymentError as exc:
            raise NatsDeploymentError(
                f"failed to create stream {stream.name!r}: {exc}"
            ) from exc
