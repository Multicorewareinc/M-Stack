---
name: queue
description: Use this skill when the user's request mentions NATS, JetStream, the event backbone, a message queue, event streams, gateway.events, or event_backbone_url. Covers the real MultiStack SDK Queue/QueueBackend classes.
---

# MultiStack SDK — Queue / QueueBackend (NATS JetStream)

Real class signature (from `multistack`, re-exported at the top level as
`from multistack import Queue, QueueBackend, NatsQueueOptions`). `nats`
is the only implementation: NATS with JetStream, through the official
Helm chart.

```python
class Queue(CapabilitySpec):
    type: str = "nats"
    kubeconfig_path: str                    # required, no ambient fallback
    namespace: Optional[str] = None         # defaults to "platform"
    options: Optional[NatsQueueOptions] = None
    storage_class: Optional[str] = None     # where JetStream persists

class NatsQueueOptions(BaseModel):
    release_name: str = "nats"
    chart: str = "nats"
    chart_version: Optional[str] = None
    replicas: int = 3                       # JetStream needs a quorum
    pvc_size: str = "20Gi"
    file_store_max_size: str = "15Gi"
    memory_store_max_size: str = "2Gi"
    image_tag: str = "2.14.6-alpine"
    extra_values: Dict[str, Any] = {}
```

## What it is in the platform

The event backbone. `QueueBackend.create()` installs NATS and creates the
platform's two streams:

- `gateway.events` (raw) — the Model Gateway publishes one event per
  response. The RPM rate limiter and the Enricher consume it.
- `gateway.events.enriched` — the Enricher republishes each event with a
  guaranteed token count (using the Tokenizer as its fallback). The TPM
  rate limiter and Billing consume it.

The enriched stream only carries events once the Enricher and Tokenizer
are running, so anything that reads it (TPM limiting, billing) needs the
queue, the gateway, the Enricher and the Tokenizer.

## Rules

- `REQUIRES = ("cluster", "storage")` — it persists to a StorageClass.
  Without one, every retained event is lost on a pod reschedule.
- Its `endpoint` (`nats://nats.platform.svc.cluster.local:4222` by
  default) is the `event_backbone_url` the gateway, both rate limiters,
  the enricher and billing point at. In `build_full_stack_plan`, composing
  `queue` wires it into all of them.
- Leave `options` unset unless the user asks for something specific —
  it fills from `type` with `NatsQueueOptions`. Don't lower `replicas`
  below 3 on your own: JetStream loses its quorum.
- `namespace` defaults to `platform`, where every consumer's default
  `event_backbone_url` already points. Only change it when the user asks.
