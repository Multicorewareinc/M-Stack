"""The cache capability: an in-cluster key-value store something else reads.

    Cache(kubeconfig_path=kc, name="cache")

`valkey` is the only implementation today, deployed from the Bitnami
chart through `multistack.helm`. The capability is named for what a
dependent asks for rather than what provides it -- `stack.py` has
recorded this as `cache` since before there was a capability to go with
it, and `Policy.cache_url` reads that key, not a Valkey.

Migrated from the `core/` + `backends/` split (`core/valkey.py`,
`backends/valkey_client.py`), which is why the spec class is `Cache`
rather than `Valkey`: a spec names the capability, an implementation is
named by `type`, the same way `Storage(type="longhorn")` does.
"""
from copy import deepcopy
from typing import Any, ClassVar, Dict, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..capability import CapabilitySpec

SUPPORTED_TYPES: Tuple[str, ...] = ("valkey",)

# The generic Helm layer owns repository resolution. A cache only needs
# to identify the chart that should be deployed.
DEFAULT_CHART = "valkey"
DEFAULT_NAMESPACE = "valkey"

# The chart's default for both the primary and the sentinel service.
DEFAULT_PORT = 6379


class ValkeyOptions(BaseModel):
    """Chart and values settings for the Bitnami Valkey chart."""

    model_config = ConfigDict(extra="forbid")

    chart: str = DEFAULT_CHART
    chart_version: Optional[str] = None

    # Chart-native Helm values. A dictionary rather than a modelled
    # schema so a caller can use any value the chart supports without
    # this model duplicating it.
    values: Dict[str, Any] = Field(default_factory=dict)

    def validate(self) -> None:
        if not self.chart:
            raise ValueError("chart is required to deploy a cache.")


class Cache(CapabilitySpec):
    """A key-value cache deployed into an existing Kubernetes cluster."""

    CAPABILITY: ClassVar[str] = "cache"
    SUPPORTED_TYPES: ClassVar[Tuple[str, ...]] = SUPPORTED_TYPES
    OPTIONS_FOR_TYPE: ClassVar[Dict[str, type]] = {"valkey": ValkeyOptions}
    DEFAULT_NAMESPACES: ClassVar[Dict[str, str]] = {"valkey": DEFAULT_NAMESPACE}
    REQUIRES: ClassVar[Tuple[str, ...]] = ("cluster",)

    FROM_STACK: ClassVar[Dict[str, str]] = {
        "kubeconfig_path": "kubeconfig_path",
    }
    # `cache_url` is the key Policy already reads from a Stack. Recording
    # a Cache is what fills it in.
    PROVIDES: ClassVar[Dict[str, str]] = {"cache_url": "endpoint"}

    type: str = "valkey"
    kubeconfig_path: str
    namespace: Optional[str] = None
    options: Optional[ValkeyOptions] = None

    # Helm release identity.
    name: str

    @model_validator(mode="after")
    def _validate_on_construction(self) -> "Cache":
        """Runs `validate()` at construction, and again on assignment.

        `CapabilitySpec`'s own validator runs `validate_capability()`
        only, so without this the rules below would not fire until a
        backend dispatched -- a spec could sit for minutes in a state it
        would later reject. Same shape as `IngressGateway`'s.
        """
        self.validate()
        return self

    def validate(self) -> None:
        self.validate_capability()

        if not self.kubeconfig_path:
            raise ValueError(
                "kubeconfig_path is required — a cache deploys into an "
                "existing Kubernetes cluster and does not fall back to "
                "an ambient kubeconfig."
            )

        if not self.name:
            raise ValueError(
                "name is required to identify the cache's Helm release."
            )

    # -- what a consumer needs to reach this ------------------------------
    @property
    def service_name(self) -> str:
        """The Service a client connects to.

        Not simply the release name. Bitnami's `common.names.fullname`
        uses the release name when it already contains the chart name
        and prefixes it otherwise, so release "valkey-test" gives
        "valkey-test" while release "cache" gives "cache-valkey". Get
        that wrong and a consumer fails DNS resolution at request time
        instead of failing to wire at build time.

        Sentinel is the other split. With it enabled the chart drops the
        primary/replica services for a single one that proxies to
        whichever pod currently holds the primary role; without it the
        writable endpoint is `-primary`, for both the standalone and the
        replication architectures.
        """
        full = self._fullname()

        if self._value(("sentinel", "enabled"), False):
            return full

        return f"{full}-primary"

    @property
    def port(self) -> int:
        """The port the writable Service listens on."""

        if self._value(("sentinel", "enabled"), False):
            return int(self._value(
                ("sentinel", "service", "ports", "valkey"), DEFAULT_PORT))

        return int(self._value(
            ("primary", "service", "ports", "valkey"), DEFAULT_PORT))

    @property
    def endpoint(self) -> str:
        """In-cluster URL for this cache, with no credential in it.

        `redis://` because that is the protocol Valkey speaks -- it is a
        fork of Redis, and the client libraries, including the one the
        rate limiters use, register that scheme.

        The password is deliberately absent even when auth is enabled.
        This is the value a consumer puts in non-secret configuration --
        the rate limiters put it straight into a ConfigMap -- and a URL
        carrying a password would put the password there too. Something
        that must authenticate overrides this with its own Secret, which
        is why those charts list their secretRef after their
        configMapRef.

        Database 0 is the chart's own default, and sharing it is safe
        here: consumers key their own data (`rl:rpm:`, `rl:tpm:`), so
        two of them in one database do not collide.
        """
        return (
            f"redis://{self.service_name}.{self.resolved_namespace}"
            f".svc.cluster.local:{self.port}/0"
        )

    # -- chart values ------------------------------------------------------
    @property
    def chart(self) -> str:
        """The chart this deploys, off the chosen implementation's options."""
        return self.options.chart

    @property
    def chart_version(self) -> Optional[str]:
        return self.options.chart_version

    @property
    def values(self) -> Dict[str, Any]:
        return self.options.values

    def helm_values(self) -> Dict[str, Any]:
        """The values passed to the chart.

        A copy, so backend or Helm processing cannot mutate the
        declarative configuration.
        """
        return deepcopy(self.options.values)

    def update_values(
        self,
        overrides: Dict[str, Any],
        *,
        replace: bool = False,
    ) -> None:
        """Merge `overrides` into the chart values, or replace them.

        Recursive merge by default, so a partial update preserves
        configuration it does not mention.
        """
        if replace:
            self.options.values = deepcopy(overrides)
            return

        self.options.values = self._deep_merge(self.options.values, overrides)

    def _fullname(self) -> str:
        """The chart's `common.names.fullname` for this release."""

        override = self._value(("fullnameOverride",), "")

        if override:
            return str(override)

        # `.Chart.Name`, which is the last segment of the chart
        # reference for a repository name, an OCI reference or a path.
        name = (
            self._value(("nameOverride",), "")
            or self.chart.rstrip("/").rsplit("/", 1)[-1]
        )

        if name in self.name:
            return self.name

        return f"{self.name}-{name}"

    def _value(self, path: tuple, default: Any) -> Any:
        """Read a nested Helm value, or `default` when it is not set."""

        current: Any = self.options.values if self.options else {}

        for key in path:
            if not isinstance(current, dict) or key not in current:
                return default
            current = current[key]

        return default if current is None else current

    @staticmethod
    def _deep_merge(
        base: Dict[str, Any],
        overrides: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Recursively merge Helm value overrides.

        Dictionaries merge; scalars, lists and everything else are
        replaced. Neither input is modified.
        """

        merged = deepcopy(base)

        for key, value in overrides.items():
            if (
                key in merged
                and isinstance(merged[key], dict)
                and isinstance(value, dict)
            ):
                merged[key] = Cache._deep_merge(merged[key], value)
            else:
                merged[key] = deepcopy(value)

        return merged
