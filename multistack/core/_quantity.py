"""Kubernetes resource-quantity parsing and credential generation shared by
every object-storage spec in this package (MinIOTenant, SeaweedFSCluster).

Split out rather than left duplicated in each: both specs need to turn a
`volume_size`-style string into bytes and to generate an access/secret
key pair when none is given, and a fix to either (the parsing regex, the
credential alphabet) belongs in one place, not two copies that can drift.
"""
from __future__ import annotations

import re
import secrets
import string

_QUANTITY_UNITS = {
    "": 1,
    "K": 10**3, "M": 10**6, "G": 10**9, "T": 10**12, "P": 10**15,
    "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50,
}


def parse_quantity(qty: str) -> int:
    """Parse a Kubernetes resource quantity (e.g. '20Gi', '500M') into bytes."""
    match = re.match(r"^([0-9.]+)\s*([A-Za-z]*)$", qty.strip())
    if not match:
        raise ValueError(f"Cannot parse Kubernetes quantity: {qty!r}")
    value, unit = match.groups()
    if unit not in _QUANTITY_UNITS:
        raise ValueError(f"Unknown unit in Kubernetes quantity: {qty!r}")
    return int(float(value) * _QUANTITY_UNITS[unit])


def _generate_credential(length: int) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))
