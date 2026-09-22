"""
The Helm layer's value objects.

Frozen models, and ours rather than pyhelm3's. `_build_release` in
manager.py converts a pyhelm3 revision into a `HelmRelease` here, which is
what keeps pyhelm3's own types off this SDK's public surface — a change
upstream becomes one file's problem instead of an API break.
"""
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict


class ReleaseStatus(str, Enum):
    UNKNOWN = "unknown"
    DEPLOYED = "deployed"
    FAILED = "failed"
    PENDING_INSTALL = "pending-install"
    PENDING_UPGRADE = "pending-upgrade"
    PENDING_ROLLBACK = "pending-rollback"
    UNINSTALLING = "uninstalling"
    UNINSTALLED = "uninstalled"


class HelmRepository(BaseModel):
    """
    A chart repository the resolver will search.

    Order is significant where several are given — see repositories.py.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    url: str


class ResolvedChart(BaseModel):
    """
    Internal result produced after resolving chart_name against the
    repositories a resolver was given.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    repository: HelmRepository
    version: Optional[str] = None


class ChartRef(BaseModel):
    """A chart named by an explicit reference instead of resolved by name.

    `oci://harbor.example/platform/model-gateway` is a complete address:
    which registry, which project, which chart. There is nothing to
    search, and nothing an index could tell us -- an OCI registry has no
    index.yaml, which is why Harbor charts cannot go through
    ChartResolver at all.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: str
    version: Optional[str] = None


class HelmRelease(BaseModel):
    """A release as it currently stands on the cluster."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    namespace: Optional[str]
    revision: Optional[int]
    status: ReleaseStatus

    chart_name: Optional[str] = None
    chart_version: Optional[str] = None


class HelmOperationResult(BaseModel):
    """What an install, upgrade or uninstall produced."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: str
    release: HelmRelease
