"""
Configuration for the Helm layer.

`kubeconfig` is required and has no default. Helm resolves `$KUBECONFIG`
and `~/.kube/config` on its own when not told which cluster to use, and a
stale one silently targets a cluster that may no longer exist — an x509
error several minutes into a chart install is how that presents. Every
other component in this SDK takes an explicit kubeconfig for the same
reason.
"""
from pathlib import Path

from pydantic import BaseModel, ConfigDict, model_validator
from typing import Optional

from .errors import HelmConfigurationError


class HelmConfig(BaseModel):
    """Configuration used by HelmManager."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kubeconfig: Path
    executable: str = "helm"
    kubecontext: Optional[str] = None
    default_timeout: str = "5m"
    history_max_revisions: int = 10
    insecure_skip_tls_verify: bool = False
    # Timeout, in seconds, used while reading Helm repository metadata
    # such as index.yaml.
    repository_timeout: int = 10

    @model_validator(mode="after")
    def _check(self) -> "HelmConfig":
        if not self.kubeconfig:
            raise HelmConfigurationError(
                "kubeconfig is required — the Helm layer will not fall back "
                "to ambient $KUBECONFIG / ~/.kube/config resolution, which "
                "can silently act on the wrong cluster."
            )
        if not self.executable:
            raise HelmConfigurationError("Helm executable cannot be empty.")
        if self.history_max_revisions < 1:
            raise HelmConfigurationError("history_max_revisions must be >= 1.")
        return self
