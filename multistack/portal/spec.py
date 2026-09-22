"""The portal capability: the single-page web UIs.

    Portal(type="admin", kubeconfig_path=kc,
           api_upstream="http://admin-control-plane.platform:8000")

Both portals are the same chart -- static files served by nginx, which
also reverse-proxies the API so the browser makes same-origin requests --
deployed twice with a different image and a different upstream. `type`
selects which portal, not which implementation.

The proxy is the reason this is a capability rather than a plain chart
install. `config.apiPrefixes` decides which paths reach the API and which
fall through to the SPA, and a path missing from that list does not fail:
it returns 200 with the index.html body, and the caller sees a JSON parse
error somewhere else entirely.
"""
from typing import ClassVar, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..capability import CapabilitySpec

SUPPORTED_TYPES: Tuple[str, ...] = ("admin", "organization")

DEFAULT_PORT = 80

# Both prefixes are needed, not one. The generated client calls /v1/ for
# most routes but hits /api/auth/refresh directly, and a refresh that
# falls through to the SPA returns 200 with HTML -- a logged-out user with
# no error anywhere.
DEFAULT_API_PREFIXES: Tuple[str, ...] = ("/v1/", "/api/")

# The cluster DNS Service IP. nginx resolves its own `resolver` at
# startup and `nginx -t` rejects a name here, so this must be an address.
DEFAULT_RESOLVER = "10.43.0.10"


class PortalOptions(BaseModel):
    """Chart/release settings for one portal."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    release_name: str
    image_repository: str
    chart: str = "ui/chart"
    chart_version: Optional[str] = None
    image_tag: Optional[str] = None

    @field_validator("release_name", "chart", "image_repository")
    @classmethod
    def _not_empty(cls, value: str, info) -> str:
        if not value:
            raise ValueError(f"{info.field_name} must not be empty")
        return value

    def validate(self) -> None:
        self.model_validate(self.model_dump())


# Both are spelled out rather than left to the chart, because `ui/chart`
# has to default to one of them and defaults to the organization portal —
# so the admin portal would otherwise silently deploy the wrong image.
# Keep these in step with the chart's own `image.repository`.
class AdminPortalOptions(PortalOptions):
    release_name: str = "admin-portal"
    image_repository: str = "ghcr.io/multicorewareinc/admin-portal"


class OrganizationPortalOptions(PortalOptions):
    release_name: str = "organization-portal"
    image_repository: str = "ghcr.io/multicorewareinc/organization-portal"


OPTIONS_FOR_TYPE = {
    "admin": AdminPortalOptions,
    "organization": OrganizationPortalOptions,
}


class Portal(CapabilitySpec):
    """A single-page portal served by nginx on a Kubernetes cluster."""

    CAPABILITY: ClassVar[str] = "portal"
    SUPPORTED_TYPES: ClassVar[Tuple[str, ...]] = SUPPORTED_TYPES
    OPTIONS_FOR_TYPE: ClassVar[Dict[str, type]] = OPTIONS_FOR_TYPE
    DEFAULT_NAMESPACES: ClassVar[Dict[str, str]] = {
        "admin": "frontend", "organization": "frontend",
    }

    REQUIRES: ClassVar[Tuple[str, ...]] = ("cluster",)
    # api_upstream is filled from a recorded ControlPlane when there is
    # one. It is optional rather than required because a portal can be
    # deployed ahead of its API -- see allow_no_api.
    FROM_STACK: ClassVar[Dict[str, str]] = {
        "kubeconfig_path": "kubeconfig_path",
        "api_upstream": "controlplane_endpoint",
    }
    PROVIDES: ClassVar[Dict[str, str]] = {"portal_endpoint": "endpoint"}

    type: str = "admin"
    kubeconfig_path: str
    namespace: Optional[str] = None
    options: Optional[PortalOptions] = None

    # Where nginx proxies API requests. Empty is allowed only with
    # allow_no_api, because the silent failure is worse than the loud one:
    # with no upstream every API call falls through to the SPA and returns
    # HTML with a 200.
    api_upstream: str = ""
    allow_no_api: bool = False
    api_prefixes: List[str] = Field(
        default_factory=lambda: list(DEFAULT_API_PREFIXES)
    )

    # Where nginx proxies /mg/* -- the organization portal's chat feature
    # calls the model gateway directly (a separate upstream from
    # api_upstream, under a separate prefix), never through the control
    # plane. Empty renders no /mg location at all: a call to it falls
    # through to the SPA fallback and returns 200 with index.html, same
    # silent-HTML failure mode api_upstream's own docstring describes for
    # a missing api_prefix -- so chat's "Unable to load models" is that
    # same failure with no error anywhere to explain it. Optional because
    # the admin portal, and an organization portal deployed before chat
    # is wired up, both have no gateway to point at.
    gateway_upstream: str = ""

    replicas: int = Field(default=1, ge=1)
    service_port: int = Field(default=DEFAULT_PORT, gt=0, le=65535)
    log_level: str = "notice"
    resolver: str = DEFAULT_RESOLVER

    # The image is built locally and sideloaded into one node's
    # containerd, so the pod has to land on that node or it cannot pull.
    node_selector: Optional[Dict[str, str]] = None

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

    @field_validator("resolver")
    @classmethod
    def _resolver_is_an_address(cls, value: str) -> str:
        # nginx resolves its own resolver at startup, and `nginx -t`
        # rejects a name outright: "host not found in resolver".
        if not value or not value.replace(".", "").replace(":", "").isalnum():
            raise ValueError(f"resolver must be an IP address, got {value!r}")
        if not any(c.isdigit() for c in value):
            raise ValueError(
                f"resolver must be an IP address, not a name, got {value!r} — "
                "nginx resolves this itself at startup and `nginx -t` fails "
                "with 'host not found in resolver'."
            )
        return value

    @field_validator("api_prefixes")
    @classmethod
    def _prefixes_are_paths(cls, value: List[str]) -> List[str]:
        for prefix in value:
            if not prefix.startswith("/"):
                raise ValueError(
                    f"api_prefix {prefix!r} must start with '/' — it is an "
                    "nginx location, not a hostname."
                )
        return value

    def validate(self) -> None:
        self.validate_capability()
        if not self.api_upstream and not self.allow_no_api:
            raise ValueError(
                "api_upstream is empty. Without it every API request falls "
                "through to the SPA and returns 200 with the index.html body, "
                "so the browser fails on a JSON parse error with nothing in "
                "any log to explain it. Set api_upstream, or set "
                "allow_no_api=True if you mean to deploy the UI before its "
                "API exists."
            )

    @property
    def release_name(self) -> str:
        return self.options.release_name if self.options else f"{self.type}-portal"

    @property
    def endpoint(self) -> str:
        """In-cluster URL for the portal itself."""
        return (
            f"http://{self.release_name}.{self.resolved_namespace}"
            f".svc.cluster.local:{self.service_port}"
        )
