"""Declarative resource model, one module per domain (see `core/rke2.py`).

This package's `__all__` is the stable import surface — add new domains'
resources to it as they land, so callers keep importing from
`multistack`/`multistack.core` rather than from individual modules.

Closed to new work: what remains here is what has not yet moved to the
per-capability packages described in docs/structure.md.
"""
from .minio import MinIOTenant
from .rke2 import RKE2Cluster, RKE2Node

__all__ = [
    "MinIOTenant",
    "RKE2Cluster",
    "RKE2Node",
]
