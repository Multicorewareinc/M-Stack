"""One module per component, doing the actual provisioning (rke2_client.py,
minio_client.py) plus the shared node-command transport (transport.py).
Import from a specific submodule -- this package re-exports nothing of its
own, so `RKE2Cluster` etc. come from `multistack` or `multistack.core`
directly.

Closed to new work, like `core/`. `transport.py` is the exception: it is
shared machinery the migrated capabilities still import.
"""
