"""The tokenizer capability: token counting for usage and quota.

    Tokenizer(kubeconfig_path=kc)

A small HTTP service that counts tokens for a model's encoding. It exists
because something downstream has to answer "how many tokens was that?"
for a response the upstream did not report `usage` for.

That consumer used to be the TPM rate limiter directly; since ADR-030 it
is the `enricher` service (TOKENIZER_URL in its own settings), which
guarantees a usable count on every event before anything else -- TPM and
billing both consume its output and neither calls the tokenizer itself
anymore. `enricher` has no SDK capability of its own yet, so this spec's
`endpoint` is wired to it by hand rather than through `Policy.FROM_STACK`
or `Enricher.FROM_STACK`.

`tiktoken` is the only implementation. A second one -- a
SentencePiece-based service for models tiktoken does not cover -- would be
another `type` behind this same spec.
"""
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..capability import CapabilitySpec

SUPPORTED_TYPES: Tuple[str, ...] = ("tiktoken",)

DEFAULT_PORT = 8000


class TiktokenOptions(BaseModel):
    """Deployment settings for the first-party tokenizer."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    release_name: str = "tokenizer"
    chart: str = "api/microservices/tokenizer/chart"
    chart_version: Optional[str] = None
    image_tag: Optional[str] = None

    # The encoding used when a request does not name one. cl100k_base
    # covers the GPT-3.5/4 family; a model outside it that falls through
    # to this default is counted with the wrong tokenizer, which shows up
    # as quietly wrong billing rather than an error.
    default_encoding: str = "cl100k_base"

    @field_validator("release_name", "chart", "default_encoding")
    @classmethod
    def _not_empty(cls, value: str, info) -> str:
        if not value:
            raise ValueError(f"{info.field_name} must not be empty")
        return value

    def validate(self) -> None:
        self.model_validate(self.model_dump())


OPTIONS_FOR_TYPE = {"tiktoken": TiktokenOptions}
TokenizerOptions = TiktokenOptions


class Tokenizer(CapabilitySpec):
    """A token-counting service on an existing Kubernetes cluster."""

    CAPABILITY: ClassVar[str] = "tokenizer"
    SUPPORTED_TYPES: ClassVar[Tuple[str, ...]] = SUPPORTED_TYPES
    OPTIONS_FOR_TYPE: ClassVar[Dict[str, type]] = OPTIONS_FOR_TYPE
    DEFAULT_NAMESPACES: ClassVar[Dict[str, str]] = {"tiktoken": "policy"}

    REQUIRES: ClassVar[Tuple[str, ...]] = ("cluster",)
    FROM_STACK: ClassVar[Dict[str, str]] = {
        "kubeconfig_path": "kubeconfig_path",
    }
    # Consumed by the enricher service's TOKENIZER_URL (ADR-030), which
    # has no SDK capability of its own yet -- so there is no
    # Enricher.FROM_STACK to fill in automatically, and a caller wires
    # this by hand into whatever installs that chart.
    PROVIDES: ClassVar[Dict[str, str]] = {"tokenizer_url": "endpoint"}

    type: str = "tiktoken"
    kubeconfig_path: str
    namespace: Optional[str] = None
    options: Optional[TokenizerOptions] = None

    replicas: int = Field(default=2, ge=1)
    service_port: int = Field(default=DEFAULT_PORT, gt=0, le=65535)

    # Scheduling. There is no in-cluster registry, so a service image
    # exists only on the node it was imported to, and a pod scheduled
    # anywhere else stays ImagePullBackOff with nothing in the release to
    # explain it. The chart's own affinity already keeps pods off the
    # control plane; this is how a caller says *which* worker.
    node_selector: Optional[Dict[str, str]] = None
    tolerations: Optional[List[Dict[str, Any]]] = None

    @field_validator("kubeconfig_path")
    @classmethod
    def _kubeconfig_required(cls, value: str) -> str:
        if not value:
            raise ValueError(
                "kubeconfig_path is required — this deploys into an existing "
                "cluster, and the SDK won't fall back to ambient "
                "$KUBECONFIG/~/.kube/config, which can silently target the "
                "wrong cluster."
            )
        return value

    def validate(self) -> None:
        self.validate_capability()

    @property
    def release_name(self) -> str:
        return self.options.release_name if self.options else "tokenizer"

    @property
    def endpoint(self) -> str:
        """In-cluster URL, with no credential in it.

        The tokenizer is unauthenticated by design: it counts tokens and
        holds nothing worth protecting. It is reachable only inside the
        cluster, which is the boundary that matters.
        """
        return (
            f"http://{self.release_name}.{self.resolved_namespace}"
            f".svc.cluster.local:{self.service_port}"
        )
