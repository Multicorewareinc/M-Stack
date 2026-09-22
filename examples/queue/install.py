"""
Install the queue -- the event backbone -- on an existing RKE2 cluster.

    pip install -e ".[helm]"                  # from the repo root
    python3 examples/queue/install.py

`nats` is the only implementation: JetStream, through the official Helm
chart. `create()` also creates the platform's two streams
(`GATEWAY_EVENTS`, `GATEWAY_EVENTS_ENRICHED`) if they don't already
exist -- the gateway, rate limiters, enricher and billing all point at
this same backbone (`event_backbone_url`) once it exists.

Needs `helm` and `kubectl` on PATH, a reachable cluster, and a working
StorageClass -- JetStream's file store needs somewhere to persist to, or
every retained message is gone the moment the pod reschedules.

`namespace` defaults to `platform`, not this implementation's own chart
default (`nats`): this capability's own NATS already runs there, having
replaced what used to be a hand-deployed one at that same address, and
every consumer's `event_backbone_url` default already assumes it
(`nats://nats.platform.svc.cluster.local:4222`). Point this at a
different, empty namespace with MULTISTACK_QUEUE_NS if you are standing
up a fresh backbone rather than replacing the live one.
"""
import os

from multistack import NatsQueueOptions, Queue, QueueBackend

KUBECONFIG = os.path.expanduser(
    os.environ.get("MULTISTACK_KUBECONFIG", "~/.multistack/kubeconfig")
)

# None leaves Queue.DEFAULT_NAMESPACES["nats"] ("platform") in place --
# see the module docstring for why that, and not this chart's own
# default, is the right one for this cluster today.
NAMESPACE = os.environ.get("MULTISTACK_QUEUE_NS") or None

# The StorageClass JetStream's file store persists to. None asks the
# driver to fall back to "longhorn", the class every other persistent
# capability in this repo's examples defaults to.
STORAGE_CLASS = os.environ.get("MULTISTACK_STORAGE_CLASS", "longhorn")

queue = Queue(
    kubeconfig_path=KUBECONFIG,
    namespace=NAMESPACE,
    storage_class=STORAGE_CLASS,
    options=NatsQueueOptions(
        # 3 is the HA topology this implementation targets. JetStream
        # refuses below 2 -- there is no quorum to keep a stream with.
        replicas=int(os.environ.get("MULTISTACK_QUEUE_REPLICAS", "3")),
    ),
)

backend = QueueBackend()
for warning in backend.check_prerequisites(queue):
    print(f"[queue] warning: {warning}")

endpoint = backend.create(queue)

print(f"\nQueue installed ({queue.type})")
print(f"  endpoint: {endpoint}")
print(f"  streams:  {', '.join(backend.list_streams(queue))}")
print(
    "\nConsumers bind themselves once gateway/rpm/tpm/enricher/billing "
    "are deployed and pointed at this endpoint -- check with:\n"
    "  backend.list_consumers(queue, 'GATEWAY_EVENTS')\n"
    "  backend.consumer_info(queue, 'GATEWAY_EVENTS', '<name>')"
)
