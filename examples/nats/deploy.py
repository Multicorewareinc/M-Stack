"""
Deploy NATS with JetStream using the Multistack SDK.

Deploys a highly available NATS cluster with JetStream persistent
storage into an existing RKE2 cluster using the official NATS Helm chart,
then creates the platform's two JetStream streams and deploys the manual
admin client (`nats-box`) used to inspect them.

    pip install -e .                          # from the repo root
    python3 examples/nats/deploy.py

Needs `helm` and `kubectl` on PATH, an existing RKE2 cluster kubeconfig,
and the configured StorageClass.

Consumers are deliberately NOT created here — a durable *push* consumer
needs a `deliver_subject` the client generates itself at bind time, so
gateway/rpm/tpm/enricher/billing each create and bind their own at
startup. See `multistack.nats.deployment.consumers.NatsConsumerInspector`
for the read-only visibility this SDK does own.
"""

import os

from multistack.nats.deployment import (
    NatsDeployment,
    NatsDeploymentManager,
)


# Do not rely on ambient $KUBECONFIG. The deployment is always explicitly
# bound to the cluster specified here.
KUBECONFIG = os.path.expanduser(
    os.environ.get(
        "MULTISTACK_KUBECONFIG",
        "~/.multistack/kubeconfig",
    )
)


deployment = NatsDeployment(
    # Required — NATS is deployed into an existing RKE2 cluster.
    kubeconfig_path=KUBECONFIG,

    release_name="nats",
    namespace="nats",

    # Three NATS replicas provide the initial HA cluster topology.
    replicas=3,

    # JetStream provides persistent messaging.
    jetstream_enabled=True,

    # Longhorn is installed in the RKE2 cluster and has been verified
    # to support dynamic PVC provisioning.
    storage_class="longhorn",
    pvc_size="20Gi",
    file_store_max_size="15Gi",

    # JetStream memory-backed storage limit.
    memory_store_max_size="2Gi",
)


manager = NatsDeploymentManager()
info = manager.create(deployment)

print(f"\nNATS {info.release_name} is {info.status}")
print(f"  namespace: {info.namespace}")
print(f"  client:    {info.client_url}")
print(f"  monitor:   {info.monitor_url}")
print(f"  metrics:   {info.metrics_url}")

# Idempotent: creates GATEWAY_EVENTS/GATEWAY_EVENTS_ENRICHED if they
# don't already exist, and deploys the nats-box admin pod alongside them.
# Safe to re-run — a stream or the pod already existing is a no-op.
manager.ensure_streams(deployment)
print("\nStreams:")
for name in manager.streams.list(deployment):
    print(f"  {name}")

print(
    "\nConsumers bind themselves once gateway/rpm/tpm/enricher/billing "
    "are deployed and pointed at this NATS — check with:\n"
    f"  manager.list_consumers(deployment, 'GATEWAY_EVENTS')\n"
    f"  manager.consumer_info(deployment, 'GATEWAY_EVENTS', '<name>')"
)