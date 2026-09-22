"""
RKE2 declarative resource model.

One file per resource domain under `core/`, mirroring the one-file-per-
backend layout under `backends/` — so a new domain (inference, storage,
accelerators, ...) is a new module here rather than another edit to a
shared file. Import these from `multistack` (or `multistack.core`), which
re-exports them; that stays the stable public surface as domains are added.

An RKE2Cluster is a cluster-level resource, provisioned before a
Kubernetes API server exists — unlike a typical Kubernetes resource,
there's no live API to apply it against.
"""
from __future__ import annotations
from typing import ClassVar, List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RKE2Node(BaseModel):
    """A node used to build an RKE2 cluster."""
    model_config = ConfigDict(
        # A misspelled field name is a typo, not a value to keep.
        extra="forbid",
        # Re-checks on assignment, so mutating a spec into an invalid
        # state fails where it happens rather than at deploy time.
        validate_assignment=True,
        arbitrary_types_allowed=True,
    )

    address: str
    user: str = "root"
    role: str = "server"  # server or agent
    ssh_key: Optional[str] = None
    ssh_port: int = 22


class RKE2Cluster(BaseModel):
    """
    Declarative definition of an RKE2 cluster.

    Unlike Kubernetes Resource objects, this is a cluster-level resource
    and is provisioned before a Kubernetes API server exists.
    """
    model_config = ConfigDict(
        # A misspelled field name is a typo, not a value to keep.
        extra="forbid",
        # Re-checks on assignment, so mutating a spec into an invalid
        # state fails where it happens rather than at deploy time.
        validate_assignment=True,
        arbitrary_types_allowed=True,
    )

    name: str
    version: str = "v1.33.1+rke2r1"
    nodes: List[RKE2Node] = Field(default_factory=list)
    token: Optional[str] = None
    kubeconfig_path: Optional[str] = None
    # One of RKE2's four bundled CNI plugins, or "none" to bring your own.
    # https://docs.rke2.io/networking/basic_network_options
    cni: str = "canal"
    # Replaces kube-proxy with Cilium's eBPF implementation. Only
    # meaningful (and only supported by this SDK) with cni="cilium" — it
    # requires a HelmChartConfig staged before rke2-server first starts,
    # which RKE2Backend handles when this is True.
    disable_kube_proxy: bool = False
    # Pod-network MTU for Cilium. Leave None to let Cilium derive it from
    # the node interface (device MTU minus 50 bytes of VXLAN overhead).
    #
    # Set it when the path between nodes carries less than the interface
    # claims — a VPN, tunnel or router between subnets, say. Cilium then
    # sends packets the underlay silently drops, and because they're large,
    # small traffic (pings, TCP handshakes) still works: connections
    # establish and then hang mid-transfer. It presents as timeouts and
    # partial failures rather than as a network outage, which makes it
    # expensive to diagnose. Measure the real path MTU with
    # `ping -M do -s <size>` between nodes and subtract 50.
    cilium_mtu: Optional[int] = None
    # Makes each node's declared `address` authoritative, by writing it as
    # `node-ip` (and `advertise-address` on servers) into config.yaml.
    #
    # Left to itself on a multi-homed host, RKE2 auto-detects the node IP
    # and can pick an interface other than the one you named here — so a
    # cluster declared on 192.0.2.x can end up registering, advertising
    # and tunnelling over 10.0.x.x instead. Nodes that can't route to that
    # other network then fail to establish their supervisor tunnel, which
    # breaks `kubectl logs`/`exec` and anything that depends on them, while
    # the node still reports Ready. Pinning it keeps the spec honest.
    pin_node_ip: bool = True

    # The bottom of the stack: it produces the kubeconfig everything
    # else needs, so it requires nothing and publishes the one value that
    # threads through every layer above.
    REQUIRES: ClassVar[tuple] = ()
    # It both takes and publishes the kubeconfig path, which is not a
    # contradiction: the path is *where to write* the file, chosen by the
    # caller, and after create() it is where the file now is. Declaring
    # only PROVIDES left it None under a Stack, so _write_kubeconfig_file
    # silently wrote nothing and every later layer failed to find it.
    FROM_STACK: ClassVar[dict] = {"kubeconfig_path": "kubeconfig_path"}
    PROVIDES: ClassVar[dict] = {"kubeconfig_path": "kubeconfig_path"}

    SUPPORTED_CNI: ClassVar[tuple] = ("canal", "cilium", "calico", "flannel", "none")

    @model_validator(mode="after")
    def _validate_on_construction(self):
        """Runs validate() at construction, and again on any assignment.

        The point of the model: a spec that exists is a spec that passed.
        validate() stays public because backends call it and it reads well
        at a call site, but it can no longer be the first time anything is
        checked.
        """
        self.validate()
        return self

    def validate(self) -> None:
        """Checks structural validity before anything touches a real
        machine: at least one node and one server, valid roles, a
        supported `cni`, `disable_kube_proxy` only paired with
        `cni="cilium"`, and no duplicate node addresses. Raises
        `ValueError` with a specific message on the first problem found."""
        if not self.nodes:
            raise ValueError("At least one RKE2 node is required")

        servers = [n for n in self.nodes if n.role == "server"]

        if not servers:
            raise ValueError("At least one RKE2 server node is required")

        for node in self.nodes:
            if node.role not in ("server", "agent"):
                raise ValueError(
                    f"Invalid RKE2 node role '{node.role}'. "
                    "Expected 'server' or 'agent'."
                )

        if self.cni not in self.SUPPORTED_CNI:
            raise ValueError(
                f"Invalid cni '{self.cni}'. Expected one of {self.SUPPORTED_CNI}."
            )

        if self.disable_kube_proxy and self.cni != "cilium":
            raise ValueError(
                "disable_kube_proxy=True requires cni='cilium' — this SDK "
                "only knows how to stage the kube-proxy-replacement "
                "HelmChartConfig for Cilium."
            )

        if self.cilium_mtu is not None:
            if self.cni != "cilium":
                raise ValueError(
                    f"cilium_mtu is set but cni is '{self.cni}' — it's applied "
                    "through Cilium's HelmChartConfig, so it only means "
                    "something with cni='cilium'."
                )
            # 576 is IPv4's guaranteed minimum; anything at or below the
            # VXLAN overhead is nonsense.
            if not 576 <= self.cilium_mtu <= 9000:
                raise ValueError(
                    f"cilium_mtu={self.cilium_mtu} is outside the plausible "
                    "range 576-9000"
                )

        addresses = [n.address for n in self.nodes]
        duplicates = {a for a in addresses if addresses.count(a) > 1}
        if duplicates:
            raise ValueError(
                f"Duplicate node address(es) in cluster.nodes: {sorted(duplicates)}. "
                "Each node must have a unique address — the backend uses "
                "address as the node's identity for join/diff/upgrade "
                "logic, so two entries sharing one address (e.g. testing "
                "'server' and 'agent' roles both on 127.0.0.1) will be "
                "silently merged together instead of treated as two nodes."
            )

    @property
    def server_nodes(self) -> List[RKE2Node]:
        """All nodes with `role == "server"`."""
        return [n for n in self.nodes if n.role == "server"]

    @property
    def agent_nodes(self) -> List[RKE2Node]:
        """All nodes with `role == "agent"`."""
        return [n for n in self.nodes if n.role == "agent"]

    @property
    def first_server(self) -> RKE2Node:
        """The node RKE2 is bootstrapped from — the first entry in
        `server_nodes`, in `nodes` order."""
        return self.server_nodes[0]
