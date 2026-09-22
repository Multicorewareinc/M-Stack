"""Which implementation serves which `type`, and the backend that dispatches."""
from __future__ import annotations

from typing import Any, Dict, List

from ..capability import CapabilityBackend
from ..state.tracking import track_create, track_delete, track_update
from .base import QueueError
from .spec import Queue

DRIVERS: Dict[str, Any] = {
    "nats": ("multistack.queue.drivers.nats", "JetStreamQueueDriver"),
}


class QueueBackend(CapabilityBackend):
    """Provisions the event backbone, dispatching on the spec's `type`."""

    CAPABILITY = "queue"
    DRIVERS = DRIVERS
    ERROR_CLS = QueueError

    def check_prerequisites(self, queue: Queue) -> List[str]:
        return self.driver_for(queue).check_prerequisites(queue)

    # "queue", not "nats": the component type names the capability a
    # dependent's REQUIRES asks for, not the implementation providing
    # it -- the same reason cache records "cache" and not "valkey".
    @track_create("queue", name_of=lambda queue: queue.type)
    def create(self, queue: Queue) -> str:
        return self.driver_for(queue).create(queue)

    @track_update(name_of=lambda queue: queue.type)
    def update(self, queue: Queue) -> str:
        return self.driver_for(queue).update(queue)

    @track_delete(name_of=lambda queue: queue.type)
    def delete(self, queue: Queue) -> None:
        return self.driver_for(queue).delete(queue)

    def exists(self, queue: Queue) -> bool:
        return self.driver_for(queue).exists(queue)

    def list_streams(self, queue: Queue) -> List[str]:
        return self.driver_for(queue).list_streams(queue)

    def list_consumers(self, queue: Queue, stream: str) -> List[str]:
        return self.driver_for(queue).list_consumers(queue, stream)

    def consumer_info(self, queue: Queue, stream: str, consumer: str):
        return self.driver_for(queue).consumer_info(queue, stream, consumer)


__all__ = ["DRIVERS", "QueueBackend"]
