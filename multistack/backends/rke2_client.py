"""
RKE2 cluster provisioning backend.

Unlike K8sBackend/HelmBackend, which talk to an *existing* cluster's API,
this backend creates the cluster itself: it runs commands on each node to
install the RKE2 systemd service, wires the join token/first-server address
into config.yaml, and starts services in the right order (first server,
then remaining servers, then agents). There's no Kubernetes API to call yet
by definition, so provisioning happens the same way HelmBackend shells out
to `helm` — by shelling out.

For a node whose address is 127.0.0.1/localhost/::1, commands run directly
via a local `bash -c` — no SSH involved, and `ssh`/sshd don't need to be
installed at all. Every other node goes over SSH, which requires `ssh` on
PATH locally and passwordless (key-based) SSH access to that node. Either
way, if the executing user isn't already root, commands are wrapped in a
non-interactive `sudo -n`, since installing RKE2 and controlling its
systemd units both require root.

Before an RKE2 cluster exists there's no API server to ask "what's already
here?" — that's what `create()`/`update()`/`delete()` need to know in order
to be idempotent and to support adding/removing nodes. So this backend
keeps a small local state file per cluster under
`~/.multistack/state/<cluster-name>.json`, recording the join token,
the address of the original first server, the node list, and the RKE2
version. `create()` refuses to run if state already exists (use `update()`
instead); `update()`/`delete()` refuse to run if it doesn't.
"""
from __future__ import annotations
import json
import os
import re
import secrets
import shlex
import time
from datetime import datetime, timezone
from typing import List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..core.rke2 import RKE2Cluster, RKE2Node
from ..state.tracking import track_create, track_delete, track_update
from .transport import NodeCommandError, NodeCommandMixin, NodePrerequisiteError

INSTALL_SCRIPT_URL = "https://get.rke2.io"
CONFIG_PATH = "/etc/rancher/rke2/config.yaml"
KUBECONFIG_REMOTE_PATH = "/etc/rancher/rke2/rke2.yaml"
NODE_TOKEN_PATH = "/var/lib/rancher/rke2/server/node-token"
# RKE2 has ONE uninstall script for both servers and agents — unlike k3s,
# there is no `rke2-agent-uninstall.sh`. Its location depends on how RKE2
# was installed, so try each documented path in turn.
# https://docs.rke2.io/install/uninstall
UNINSTALL_SCRIPT_PATHS = [
    "/usr/local/bin/rke2-uninstall.sh",  # tarball (what get.rke2.io uses)
    "/usr/bin/rke2-uninstall.sh",        # RPM
    "/opt/rke2/bin/rke2-uninstall.sh",   # read-only / btrfs filesystems
]
CILIUM_HELM_CHART_CONFIG_PATH = "/var/lib/rancher/rke2/server/manifests/rke2-cilium-config.yaml"

DEFAULT_STATE_DIR = os.path.expanduser("~/.multistack/state")

# Thresholds and port lists per https://docs.rke2.io/install/requirements
# (checked 2026-08-31). Only checks that are actually verifiable over
# SSH/local-exec are implemented — disk type (SSD recommendation) and
# inotify kernel limits are advisory/workload-dependent in the docs and
# aren't hard install blockers, so they're not checked here.
SUPPORTED_ARCHES = {"x86_64", "aarch64", "arm64"}
MIN_RAM_MB = 4096
RECOMMENDED_RAM_MB = 8192
MIN_CPU_CORES = 2
RECOMMENDED_CPU_CORES = 4
# Ports RKE2 needs bound to itself; a node with something else already
# listening on one of these will fail to start.
SERVER_REQUIRED_PORTS = [6443, 9345, 2379, 2380, 2381, 10250]
AGENT_REQUIRED_PORTS = [10250]

# CNIs that carry pod traffic in a VXLAN tunnel, and the per-packet
# overhead that costs. The pod-network MTU has to fit inside the real path
# MTU between nodes once this is added, or full-size pod packets are
# silently dropped while small ones get through — connections establish,
# then hang. See RKE2Cluster.cilium_mtu.
VXLAN_CNIS = {"cilium", "canal", "flannel"}
VXLAN_OVERHEAD_BYTES = 50
ICMP_HEADER_BYTES = 28


class RKE2PrerequisiteError(NodePrerequisiteError):
    """Raised when a node fails a hard RKE2 install requirement."""


class RKE2Error(NodeCommandError):
    """Raised for any other RKE2 provisioning failure (command failure,
    timeout waiting for a service/token, aggregated multi-node failure)."""


def _utcnow() -> str:
    """Current UTC time as an ISO-8601 string, for state timestamps."""
    return datetime.now(timezone.utc).isoformat()


def _safe_filename(name: str) -> str:
    """Sanitizes `name` for use as a state filename, replacing anything
    other than letters/digits/`.`/`_`/`-` with `_`."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


class ClusterState(BaseModel):
    """Everything the backend needs to remember between calls, since RKE2
    clusters (unlike K8s resources) have no live API to query beforehand."""
    model_config = ConfigDict(
        # A misspelled field name is a typo, not a value to keep.
        extra="forbid",
        # Re-checks on assignment, so mutating a spec into an invalid
        # state fails where it happens rather than at deploy time.
        validate_assignment=True,
        arbitrary_types_allowed=True,
    )

    name: str
    version: str
    token: str
    first_server_address: str
    kubeconfig_path: Optional[str]
    cni: str = "canal"
    disable_kube_proxy: bool = False
    nodes: List[RKE2Node] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def to_json(self) -> str:
        """Serializes this state to a JSON string for on-disk storage."""
        return self.model_dump_json(indent=2)

    @classmethod
    def from_json(cls, raw: str) -> "ClusterState":
        """Deserializes a `ClusterState` from JSON produced by `to_json()`.

        Nested RKE2Nodes are rebuilt and validated by the model, which is
        what the hand-written version did by calling RKE2Node(**n) per
        entry — with no validation, so a state file with a bad role
        deserialized happily and failed later.
        """
        return cls.model_validate_json(raw)


class RKE2Backend(NodeCommandMixin):
    """Provisions, reconciles, and tears down RKE2 clusters over SSH (or
    directly, for local nodes). See the module docstring for the overall
    approach; `create()`/`update()`/`delete()` are the public entry points.

    Command execution, probes, and parallel fan-out come from
    `NodeCommandMixin` — shared with every other node-configuring backend."""

    error_cls = RKE2Error
    prerequisite_error_cls = RKE2PrerequisiteError
    log_prefix = "rke2"

    def __init__(
        self,
        ssh_timeout: int = 30,
        poll_interval: int = 5,
        poll_timeout: int = 600,
        state_dir: str = DEFAULT_STATE_DIR,
        max_parallel: int = 10,
        command_timeout: int = 900,
    ):
        """
        `ssh_timeout`: seconds to wait for an SSH connection to establish.
        `poll_interval`/`poll_timeout`: how often, and how long, to poll
        when waiting for a systemd service or the node token to appear.
        `state_dir`: where per-cluster state JSON files are stored.
        `max_parallel`: cap on concurrent SSH connections when
        bootstrapping/tearing down agent nodes in parallel (server nodes
        always run sequentially — see `_run_parallel`).
        `command_timeout`: wall-clock cap on any single remote/local
        command. Generous by default because the RKE2 install script pulls
        images; it exists so a command that hangs after connecting can't
        block a worker thread indefinitely.
        """
        self.ssh_timeout = ssh_timeout
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout
        self.state_dir = state_dir
        self.command_timeout = command_timeout
        # Caps concurrent SSH connections when bootstrapping/tearing down
        # agent nodes in parallel (server nodes always run sequentially —
        # see _run_parallel).
        self.max_parallel = max_parallel

    # -- public API -------------------------------------------------------
    @track_create("cluster", name_of=lambda cluster: cluster.name)
    def create(self, cluster: RKE2Cluster) -> str:
        """
        Provisions a brand-new cluster:
          1. installs + starts RKE2 on the first server node
          2. joins any remaining server nodes
          3. joins all agent nodes
        Refuses to run if state already exists for `cluster.name` — call
        `update()` to modify an existing cluster instead. Returns the
        kubeconfig contents (also written to `cluster.kubeconfig_path` if
        one was given).
        """
        cluster.validate()

        if self._load_state(cluster.name) is not None:
            raise RKE2Error(
                f"cluster '{cluster.name}' already has state at "
                f"{self._state_path(cluster.name)}. Use update() to modify "
                f"it, or delete() to tear it down first."
            )

        if not cluster.token:
            cluster.token = secrets.token_hex(32)

        print("[rke2] checking prerequisites")
        warnings = self.check_prerequisites(cluster.nodes, cluster.nodes, cluster)
        for w in warnings:
            print(f"[rke2] warning: {w}")

        first = cluster.first_server
        now = _utcnow()
        # Saved incrementally as each node succeeds below (via
        # _upsert_node_in_state), rather than once at the very end — so a
        # failure partway through a multi-node create() still leaves an
        # accurate record of what actually got provisioned. Without this,
        # a node that failed to join later in the same call would silently
        # strand the nodes that *did* succeed: no state file at all, so
        # update() (which needs existing state) can't reach them and
        # delete() refuses to run ("no state found"), leaving no SDK-driven
        # way to tear them back down.
        state = ClusterState(
            name=cluster.name,
            version=cluster.version,
            token=cluster.token,
            first_server_address=first.address,
            kubeconfig_path=cluster.kubeconfig_path,
            cni=cluster.cni,
            disable_kube_proxy=cluster.disable_kube_proxy,
            nodes=[],
            created_at=now,
            updated_at=now,
        )

        print(f"[rke2] bootstrapping first server {first.address}")
        self._bootstrap_server(first, cluster.token, cluster.version, cluster.cni, cluster.disable_kube_proxy, first_server=None, pin_node_ip=cluster.pin_node_ip, cilium_mtu=cluster.cilium_mtu)
        self._wait_for_node_token(first)
        self._upsert_node_in_state(state, first)

        # Written now, not at the end. The API server is up as soon as the
        # first server is, and joining the remaining nodes can take several
        # minutes — during which there was no kubeconfig on disk, so the
        # only way to watch nodes register was to ssh to the server and use
        # /etc/rancher/rke2/rke2.yaml directly. Anything that wants to
        # observe the cluster coming up can now just use the path it asked
        # for. Re-fetched at the end in case the server rewrote it.
        kubeconfig = self._fetch_kubeconfig(first)
        self._write_kubeconfig_file(cluster.kubeconfig_path, kubeconfig)
        if cluster.kubeconfig_path:
            print(f"[rke2] kubeconfig ready at {cluster.kubeconfig_path} "
                  f"({len(cluster.agent_nodes)} node(s) still joining)")

        for node in cluster.server_nodes[1:]:
            print(f"[rke2] joining server {node.address}")
            self._bootstrap_server(node, cluster.token, cluster.version, cluster.cni, cluster.disable_kube_proxy, first_server=first, pin_node_ip=cluster.pin_node_ip, cilium_mtu=cluster.cilium_mtu)
            self._upsert_node_in_state(state, node)

        if cluster.agent_nodes:
            print(f"[rke2] joining {len(cluster.agent_nodes)} agent node(s) in parallel "
                  f"(up to {min(len(cluster.agent_nodes), self.max_parallel)} at once)")
            self._run_parallel(
                cluster.agent_nodes,
                lambda node: self._bootstrap_agent(node, cluster.token, cluster.version, first, cluster.pin_node_ip),
                label="agent join",
                on_success=lambda node, _result: self._upsert_node_in_state(state, node),
            )

        kubeconfig = self._fetch_kubeconfig(first)
        self._write_kubeconfig_file(cluster.kubeconfig_path, kubeconfig)
        self._save_state(state)

        return kubeconfig

    @track_update(name_of=lambda cluster: cluster.name)
    def update(self, cluster: RKE2Cluster) -> str:
        """
        Reconciles a running cluster to match `cluster`:
          - nodes present in `cluster.nodes` but not in the saved state are
            joined
          - nodes present in the saved state but not in `cluster.nodes` are
            uninstalled and dropped
          - if `cluster.version` differs from the saved version, remaining
            nodes are upgraded one at a time (server nodes other than the
            first are done before it, so the API stays reachable as long
            as possible; this is not a zero-downtime rolling upgrade)
        Refuses to run if no state exists for `cluster.name` — call
        `create()` first. The original first server can't be removed by
        `update()`; `delete()` and `create()` a new cluster instead.
        """
        cluster.validate()

        state = self._load_state(cluster.name)
        if state is None:
            raise RKE2Error(
                f"no state found for cluster '{cluster.name}'. Use create() first."
            )

        new_addresses = {n.address for n in cluster.nodes}
        if state.first_server_address not in new_addresses:
            raise RKE2Error(
                f"the original first server ({state.first_server_address}) "
                "is missing from cluster.nodes. update() can't replace the "
                "first server — delete() this cluster and create() a new "
                "one instead."
            )

        if cluster.cni != state.cni:
            raise RKE2Error(
                f"cluster.cni changed from '{state.cni}' to '{cluster.cni}', "
                "but RKE2 doesn't support switching CNI plugins on a running "
                "cluster. delete() this cluster and create() a new one with "
                "the CNI you want instead."
            )

        if cluster.disable_kube_proxy != state.disable_kube_proxy:
            raise RKE2Error(
                "cluster.disable_kube_proxy changed, but toggling kube-proxy "
                "on a running cluster isn't supported by this SDK — it "
                "requires the HelmChartConfig to already be in place before "
                "the first server ever started. delete() this cluster and "
                "create() a new one with the setting you want instead."
            )

        token = state.token  # join token is fixed at cluster creation
        first_server = next(
            n for n in cluster.nodes if n.address == state.first_server_address
        )

        old_by_address = {n.address: n for n in state.nodes}
        added = [n for n in cluster.nodes if n.address not in old_by_address]
        removed = [n for n in state.nodes if n.address not in new_addresses]
        kept = [n for n in cluster.nodes if n.address in old_by_address]

        if added:
            print("[rke2] checking prerequisites for new node(s)")
            warnings = self.check_prerequisites(added, cluster.nodes, cluster)
            for w in warnings:
                print(f"[rke2] warning: {w}")
        else:
            # check_prerequisites() (which includes this same check) only
            # runs when there are new nodes to check — but a removal-only
            # update() can just as easily land on an even server count, so
            # check for that here too. Cheap: pure local computation, no SSH.
            server_count_warning = self._server_count_warning(cluster.nodes)
            if server_count_warning:
                print(f"[rke2] warning: {server_count_warning}")

        # Same reasoning as create(): mutate + save `state` after each node
        # rather than only once at the end, so a failure partway through
        # (e.g. node 2 of 3 new agents fails to join) doesn't discard the
        # record of the nodes that already succeeded.
        removed_agents = [n for n in removed if n.role == "agent"]
        removed_servers = [n for n in removed if n.role == "server"]
        for node in removed_servers:
            print(f"[rke2] removing server {node.address}")
            self._teardown_node(node)
            self._remove_node_from_state(state, node)
        if removed_agents:
            print(f"[rke2] removing {len(removed_agents)} agent node(s) in parallel")
            self._run_parallel(
                removed_agents,
                self._teardown_node,
                label="teardown",
                on_success=lambda node, _r: self._remove_node_from_state(state, node),
            )

        added_servers = [n for n in added if n.role == "server"]
        added_agents = [n for n in added if n.role == "agent"]
        for node in added_servers:
            print(f"[rke2] joining new server {node.address}")
            self._bootstrap_server(node, token, cluster.version, cluster.cni, cluster.disable_kube_proxy, first_server=first_server, pin_node_ip=cluster.pin_node_ip, cilium_mtu=cluster.cilium_mtu)
            self._upsert_node_in_state(state, node)
        if added_agents:
            print(f"[rke2] joining {len(added_agents)} new agent node(s) in parallel")
            self._run_parallel(
                added_agents,
                lambda node: self._bootstrap_agent(node, token, cluster.version, first_server, cluster.pin_node_ip),
                label="agent join",
                on_success=lambda node, _r: self._upsert_node_in_state(state, node),
            )

        if cluster.version != state.version:
            # Upgrade nodes that already existed before this call. Agents
            # upgrade in parallel (independent of each other); non-first
            # servers upgrade sequentially, first server last, so the
            # control plane stays up as long as possible.
            kept_agents = [n for n in kept if n.role == "agent"]
            kept_other_servers = [n for n in kept if n.role == "server" and n.address != first_server.address]
            kept_first_server = [n for n in kept if n.address == first_server.address]

            if kept_agents:
                print(f"[rke2] upgrading {len(kept_agents)} agent node(s) to {cluster.version} in parallel")
                self._run_parallel(
                    kept_agents,
                    lambda node: self._upgrade_node(node, cluster.version),
                    label="upgrade",
                )
            for node in kept_other_servers + kept_first_server:
                print(f"[rke2] upgrading {node.address} to {cluster.version}")
                self._upgrade_node(node, cluster.version)

        kubeconfig = self._fetch_kubeconfig(first_server)
        self._write_kubeconfig_file(cluster.kubeconfig_path, kubeconfig)

        # `state.nodes` is already correct — it's been maintained
        # incrementally by _upsert_node_in_state/_remove_node_from_state
        # above as each add/remove actually completed, which is more
        # trustworthy than rebuilding it from `cluster.nodes` (the
        # *desired* set) here: if an add or remove failed partway through,
        # cluster.nodes wouldn't reflect that, but state.nodes already does.
        # Only the fields update() is actually responsible for changing —
        # version (once every kept node is confirmed upgraded, above) and
        # kubeconfig_path — need setting explicitly.
        state.version = cluster.version
        state.kubeconfig_path = cluster.kubeconfig_path
        state.updated_at = _utcnow()
        self._save_state(state)

        return kubeconfig

    @track_delete(name_of=lambda cluster: cluster if isinstance(cluster, str) else cluster.name)
    def delete(self, cluster: Union[RKE2Cluster, str]) -> None:
        """
        Tears down every node in the cluster (uninstalling RKE2 via its own
        uninstall scripts) and removes the saved state. Agents go first,
        then servers other than the first, then the first server last.
        Best-effort: a node that's unreachable or already torn down won't
        block cleanup of the rest.
        """
        name = cluster.name if isinstance(cluster, RKE2Cluster) else cluster
        state = self._load_state(name)
        if state is None:
            raise RKE2Error(f"no state found for cluster '{name}'. Nothing to delete.")

        agents = [n for n in state.nodes if n.role == "agent"]
        other_servers = [n for n in state.nodes if n.role == "server" and n.address != state.first_server_address]
        first_server_node = [n for n in state.nodes if n.address == state.first_server_address]

        if agents:
            print(f"[rke2] tearing down {len(agents)} agent node(s) in parallel")
            self._run_parallel(agents, self._teardown_node, label="teardown")

        for node in other_servers + first_server_node:
            print(f"[rke2] tearing down {node.address}")
            self._teardown_node(node)

        if state.kubeconfig_path and os.path.exists(state.kubeconfig_path):
            os.remove(state.kubeconfig_path)

        self._delete_state_file(name)

    # -- state persistence ------------------------------------------------
    def _state_path(self, name: str) -> str:
        """Path to the state JSON file for cluster `name`."""
        return os.path.join(self.state_dir, f"{_safe_filename(name)}.json")

    def _load_state(self, name: str) -> Optional[ClusterState]:
        """Loads the saved state for cluster `name`, or `None` if it has none."""
        path = self._state_path(name)
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return ClusterState.from_json(f.read())

    def _save_state(self, state: ClusterState) -> None:
        """Writes `state` to its JSON file, creating `state_dir` if needed."""
        os.makedirs(self.state_dir, exist_ok=True)
        path = self._state_path(state.name)
        with open(path, "w") as f:
            f.write(state.to_json())
        # Contains the cluster join token — same treatment RKE2 itself gives
        # /etc/rancher/rke2/rke2.yaml on the node.
        os.chmod(path, 0o600)

    def _delete_state_file(self, name: str) -> None:
        """Removes the state JSON file for cluster `name`, if it exists."""
        path = self._state_path(name)
        if os.path.exists(path):
            os.remove(path)

    def _upsert_node_in_state(self, state: ClusterState, node: RKE2Node) -> None:
        """Adds `node` to `state.nodes` (replacing any existing entry with
        the same address) and saves immediately, so a node that's actually
        joined is on record even if a later node in the same create()/
        update() call fails."""
        state.nodes = [n for n in state.nodes if n.address != node.address] + [node]
        state.updated_at = _utcnow()
        self._save_state(state)

    def _remove_node_from_state(self, state: ClusterState, node: RKE2Node) -> None:
        """Drops `node` from `state.nodes` and saves immediately — the
        teardown counterpart to `_upsert_node_in_state`."""
        state.nodes = [n for n in state.nodes if n.address != node.address]
        state.updated_at = _utcnow()
        self._save_state(state)

    # -- prerequisite checks (https://docs.rke2.io/install/requirements) --
    def check_prerequisites(
        self,
        nodes_to_check: List[RKE2Node],
        all_cluster_nodes: Optional[List[RKE2Node]] = None,
        cluster: Optional[RKE2Cluster] = None,
    ) -> List[str]:
        """
        Runs RKE2's documented install requirements against `nodes_to_check`
        and returns a list of warning strings for soft issues (below
        recommended, not below minimum). Raises RKE2PrerequisiteError if
        any node hard-fails (unsupported arch, no systemd/iptables, below
        minimum RAM/CPU) or on a hostname collision. Runs concurrently
        across nodes — these checks are all independent per node.

        Hostname uniqueness is checked across `all_cluster_nodes` (defaults
        to `nodes_to_check`) since RKE2 identifies a node by its hostname
        by default, so a newly added node can collide with one that's
        already part of the cluster even if it isn't being freshly checked
        itself. Also includes the (non-node-specific) odd-server-count
        check — see `_server_count_warning`.
        """
        all_nodes = all_cluster_nodes or nodes_to_check

        hostnames = self._run_parallel(
            all_nodes,
            # Via _probe, so an unreachable node fails loudly here instead
            # of yielding "" — an empty hostname silently opts out of the
            # collision check below, so two broken nodes would both "pass".
            lambda node: self._probe(node, "hostname", "hostname"),
            label="hostname check",
        )

        hostname_owner: dict = {}
        for node in all_nodes:
            hostname = hostnames.get(node.address, "")
            if hostname and hostname in hostname_owner and hostname_owner[hostname] != node.address:
                raise RKE2PrerequisiteError(
                    f"Nodes {hostname_owner[hostname]} and {node.address} both "
                    f"report hostname '{hostname}'. RKE2 node identity defaults "
                    "to hostname, and two nodes can't share one — set a unique "
                    "hostname on one of them (or configure node-name/"
                    "with-node-id, which this SDK doesn't set automatically)."
                )
            if hostname:
                hostname_owner[hostname] = node.address

        warnings: List[str] = []
        server_count_warning = self._server_count_warning(all_nodes)
        if server_count_warning:
            warnings.append(server_count_warning)

        # Needs the spec to know which CNI and MTU are intended, so it only
        # runs when the caller passed the cluster.
        if cluster is not None:
            warnings.extend(
                self._check_pod_network_mtu(all_nodes, cluster.cni, cluster.cilium_mtu)
            )

        if not nodes_to_check:
            return warnings

        per_node_warnings = self._run_parallel(
            nodes_to_check, self._check_node_prerequisites, label="prerequisite check"
        )
        warnings += [w for ws in per_node_warnings.values() for w in ws]
        return warnings

    @staticmethod
    def _server_count_warning(nodes: List[RKE2Node]) -> Optional[str]:
        """Flags an even server-node count: etcd needs a simple majority to
        make progress, so an odd count (1, 3, 5, ...) gets fault tolerance
        an even count wastes (e.g. 4 servers tolerates the same 1 lost node
        as 3). Pure local computation — no SSH — so it's cheap enough to
        call on every create()/update(), not just when new nodes are being
        checked."""
        server_count = len([n for n in nodes if n.role == "server"])
        if server_count > 0 and server_count % 2 == 0:
            return (
                f"cluster has an even number of server nodes ({server_count}) "
                "— etcd needs a majority to make progress, so an odd count "
                "(1, 3, 5, ...) gets fault tolerance an even count wastes"
            )
        return None

    def _check_node_prerequisites(self, node: RKE2Node) -> List[str]:
        """Runs the single-node hard/soft checks documented in
        `check_prerequisites` and returns the soft-warning strings for
        `node`; hard failures raise `RKE2PrerequisiteError` directly."""
        warnings: List[str] = []

        # First, because every later step in provisioning depends on it.
        self._check_passwordless_sudo(node)

        arch = self._probe(node, "uname -m", "CPU architecture")
        if arch not in SUPPORTED_ARCHES:
            raise RKE2PrerequisiteError(
                f"{node.address}: unsupported architecture '{arch}' — "
                "RKE2 supports x86_64 and arm64/aarch64 only"
            )

        has_systemd = self._probe(
            node, "test -d /run/systemd/system && echo yes || echo no", "systemd presence"
        )
        if has_systemd != "yes":
            raise RKE2PrerequisiteError(
                f"{node.address}: systemd not detected at /run/systemd/system — "
                "RKE2 requires a systemd-based Linux distribution"
            )

        has_iptables = self._probe(
            node,
            "command -v iptables >/dev/null 2>&1 && echo yes || echo no",
            "iptables presence",
        )
        if has_iptables != "yes":
            raise RKE2PrerequisiteError(f"{node.address}: iptables not found on PATH")

        mem_mb = self._parse_int(
            self._probe(node, "free -m | awk '/^Mem:/{print $2}'", "total RAM")
        )
        if mem_mb is not None:
            if mem_mb < MIN_RAM_MB:
                raise RKE2PrerequisiteError(
                    f"{node.address}: {mem_mb}MB RAM is below RKE2's minimum of {MIN_RAM_MB}MB"
                )
            if mem_mb < RECOMMENDED_RAM_MB:
                warnings.append(
                    f"{node.address}: {mem_mb}MB RAM is below the recommended {RECOMMENDED_RAM_MB}MB"
                )

        cpu_cores = self._parse_int(self._probe(node, "nproc", "CPU core count"))
        if cpu_cores is not None:
            if cpu_cores < MIN_CPU_CORES:
                raise RKE2PrerequisiteError(
                    f"{node.address}: {cpu_cores} CPU core(s) is below RKE2's minimum of {MIN_CPU_CORES}"
                )
            if cpu_cores < RECOMMENDED_CPU_CORES:
                warnings.append(
                    f"{node.address}: {cpu_cores} CPU core(s) is below the recommended {RECOMMENDED_CPU_CORES}"
                )

        ports = SERVER_REQUIRED_PORTS if node.role == "server" else AGENT_REQUIRED_PORTS
        for port in ports:
            check_cmd = (
                f"(ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null) | "
                f"grep -q ':{port} ' && echo busy || echo free"
            )
            if self._probe(node, check_cmd, f"whether port {port} is free") == "busy":
                warnings.append(
                    f"{node.address}: port {port} is already in use by another "
                    "process — RKE2 needs this port for itself"
                )

        warnings.extend(self._check_node_addressing(node))
        return warnings

    def _measure_path_mtu(self, node: RKE2Node, target: str) -> Optional[int]:
        """Binary-searches the largest packet that survives the path from
        `node` to `target` with DF set, and returns that path MTU.

        The search runs entirely in one remote shell — a dozen pings in a
        single SSH round trip rather than a dozen round trips. Returns None
        if ICMP is blocked outright, since then this tells us nothing.
        """
        script = (
            "lo=576; hi=1472; best=0; "
            "while [ $lo -le $hi ]; do "
            "  mid=$(( (lo + hi) / 2 )); "
            f"  if ping -c1 -W1 -M do -s $mid {shlex.quote(target)} >/dev/null 2>&1; "
            "    then best=$mid; lo=$((mid + 1)); "
            "    else hi=$((mid - 1)); fi; "
            "done; echo $best"
        )
        best = self._parse_int(self._probe(node, script, "path MTU"))
        if not best:
            return None
        return best + ICMP_HEADER_BYTES

    def _check_pod_network_mtu(
        self, nodes: List[RKE2Node], cni: str, cilium_mtu: Optional[int]
    ) -> List[str]:
        """
        Warns when the path between nodes can't carry the pod-network MTU
        the CNI is going to use.

        This is the failure that looks like everything except a network
        problem: a VPN or tunnel between subnets quietly lowers the path
        MTU, the CNI still sizes itself from the local interface, and only
        full-size packets are dropped. Pings work, TCP handshakes work,
        pod-to-pod connections open and then hang — so it surfaces as
        DNS flakiness, provisioner timeouts and half-working services.
        Measuring it up front costs one SSH round trip per node.
        """
        if cni not in VXLAN_CNIS or len(nodes) < 2:
            return []

        warnings: List[str] = []
        target = nodes[0]
        for node in nodes[1:]:
            path_mtu = self._measure_path_mtu(node, target.address)
            if path_mtu is None:
                warnings.append(
                    f"{node.address}: couldn't measure the path MTU to "
                    f"{target.address} (ICMP may be blocked), so an MTU "
                    "mismatch can't be ruled out here"
                )
                continue

            usable = path_mtu - VXLAN_OVERHEAD_BYTES
            intended = cilium_mtu if cilium_mtu is not None else 1450
            if intended > usable:
                warnings.append(
                    f"{node.address}: path MTU to {target.address} is only "
                    f"{path_mtu}, which leaves {usable} for pod traffic after "
                    f"{VXLAN_OVERHEAD_BYTES} bytes of VXLAN overhead — but "
                    f"{cni} will use {intended}. Full-size pod packets will be "
                    "dropped while small ones get through, so pod-to-pod "
                    "connections will open and then hang. Set "
                    f"cilium_mtu={usable - 28} (or lower) on the cluster spec."
                )
        return warnings

    def _check_node_addressing(self, node: RKE2Node) -> List[str]:
        """Warns when a node's declared address isn't the only address it
        has, or isn't present on it at all.

        On a multi-homed host RKE2 auto-detects the node IP, and can choose
        an interface other than the declared one — producing a cluster that
        registers and tunnels over a network some nodes can't reach, while
        every node still reports Ready. `pin_node_ip` (on by default)
        prevents that; this check flags the hosts where it matters, and
        catches a declared address that belongs to no local interface.
        """
        warnings: List[str] = []
        raw = self._probe(
            node,
            "ip -4 -o addr show scope global | awk '{print $4}' | cut -d/ -f1",
            "local IPv4 addresses",
        )
        addresses = [a for a in raw.split() if a]
        if not addresses:
            return warnings

        if node.address not in addresses and not self._is_local(node):
            warnings.append(
                f"{node.address}: that address isn't on any interface of this "
                f"node (it has {', '.join(addresses)}). It's presumably NATed or "
                "forwarded, which works for SSH but means node-ip can't be "
                "pinned to it — expect RKE2 to register one of the addresses above."
            )
        elif len(addresses) > 1:
            others = [a for a in addresses if a != node.address]
            warnings.append(
                f"{node.address}: multi-homed node (also has {', '.join(others)}). "
                "With pin_node_ip=True the declared address is used; with it "
                "disabled, RKE2 may auto-select one of the others and nodes that "
                "can't route to that network will fail to establish their "
                "supervisor tunnel."
            )
        return warnings

    # -- provisioning steps -------------------------------------------
    def _bootstrap_server(
        self,
        node: RKE2Node,
        token: str,
        version: str,
        cni: str,
        disable_kube_proxy: bool,
        first_server: Optional[RKE2Node],
        pin_node_ip: bool = True,
        cilium_mtu: Optional[int] = None,
    ) -> None:
        """Installs and starts `rke2-server` on `node`: runs the install
        script, stages the Cilium HelmChartConfig first if any Cilium
        setting applies, writes config.yaml (token, cni, tls-san,
        node-ip/advertise-address, and — if `first_server` is given — the
        `server:` URL to join it), then enables/starts the service and
        waits for it to become active. `first_server=None` means `node`
        *is* the first server being bootstrapped from scratch."""
        self._install_rke2(node, role="server", version=version)

        if disable_kube_proxy or cilium_mtu is not None:
            # Must exist before rke2-server starts: kube-proxy replacement
            # can't be staged afterwards without leaving the cluster with
            # neither kube-proxy nor a replacement, and an MTU applied after
            # the fact means pods come up on the wrong one first.
            self._write_cilium_chart_config(
                node, disable_kube_proxy=disable_kube_proxy, cilium_mtu=cilium_mtu
            )

        lines = [f"token: {token}", f"cni: {cni}"]
        if disable_kube_proxy:
            lines.append("disable-kube-proxy: true")
        lines.append(f"tls-san:\n  - {node.address}")
        if pin_node_ip:
            # Both, and for a reason: node-ip fixes the address this node
            # registers and tunnels on, while advertise-address fixes the
            # address the API server hands to agents to dial back. Setting
            # only the first still lets a multi-homed server advertise an
            # interface some agents can't reach.
            lines.append(f"node-ip: {node.address}")
            lines.append(f"advertise-address: {node.address}")
        if first_server is not None:
            lines.append(f"server: https://{first_server.address}:9345")
        self._write_config(node, "\n".join(lines))

        self._run(node, "systemctl enable rke2-server.service")
        self._run(node, "systemctl start rke2-server.service")
        self._wait_for_service(node, "rke2-server")

    def _bootstrap_agent(
        self,
        node: RKE2Node,
        token: str,
        version: str,
        first_server: RKE2Node,
        pin_node_ip: bool = True,
    ) -> None:
        """Installs and starts `rke2-agent` on `node`, joining it to
        `first_server` via config.yaml's `token`/`server` fields."""
        self._install_rke2(node, role="agent", version=version)

        lines = [
            f"token: {token}",
            f"server: https://{first_server.address}:9345",
        ]
        if pin_node_ip:
            lines.append(f"node-ip: {node.address}")
        self._write_config(node, "\n".join(lines))

        self._run(node, "systemctl enable rke2-agent.service")
        self._run(node, "systemctl start rke2-agent.service")
        self._wait_for_service(node, "rke2-agent")

    def _upgrade_node(self, node: RKE2Node, version: str) -> None:
        """Re-runs the install script pinned to `version` and restarts
        `node`'s RKE2 service (server or agent, per `node.role`). Doesn't
        drain workloads first -- see `examples/rke2/upgrade.py`."""
        service = "rke2-server" if node.role == "server" else "rke2-agent"
        self._install_rke2(node, role=node.role, version=version)
        self._run(node, f"systemctl restart {service}.service")
        self._wait_for_service(node, service)

    def _teardown_node(self, node: RKE2Node) -> None:
        """Runs RKE2's own uninstall script on `node`, whichever documented
        path it lives at.

        Tolerant of a node that's unreachable or was never fully installed
        — one bad node shouldn't block the rest of a teardown. But it does
        NOT tolerate finding no uninstall script on a node that still has
        RKE2 on it: silently "succeeding" there is worse than failing,
        because `delete()` would then drop the state file and leave a live,
        untracked node running with no SDK-side record of it.
        """
        try:
            script = self._find_uninstall_script(node)
        except Exception as exc:  # unreachable host, etc.
            print(f"[rke2] warning: couldn't reach {node.address} to tear down cleanly: {exc}")
            return

        if script is None:
            if self._rke2_still_present(node):
                raise RKE2Error(
                    f"{node.address}: RKE2 is still installed but no uninstall "
                    f"script was found at any of {', '.join(UNINSTALL_SCRIPT_PATHS)}. "
                    "Refusing to report this node as torn down — it would be left "
                    "running with no state tracking it. Remove RKE2 manually, then "
                    "re-run delete()."
                )
            print(f"[rke2] {node.address}: nothing to uninstall")
            return

        try:
            out = self._run(node, script, check=False)
            print(f"[rke2] teardown output for {node.address}: {out.strip()}")
        except Exception as exc:
            print(f"[rke2] warning: couldn't reach {node.address} to tear down cleanly: {exc}")

    def _find_uninstall_script(self, node: RKE2Node) -> Optional[str]:
        """Returns the first uninstall-script path that exists on `node`, or
        None. RKE2 puts it in a different place per install method, and uses
        the same script for servers and agents."""
        found = self._run(
            node,
            " ; ".join(f"test -x {p} && echo {p}" for p in UNINSTALL_SCRIPT_PATHS),
            check=False,
        ).split()
        return found[0] if found else None

    def _rke2_still_present(self, node: RKE2Node) -> bool:
        """Whether `node` still has RKE2 installed — used to tell "already
        clean" apart from "uninstall script is missing but RKE2 isn't"."""
        return self._run(
            node,
            "command -v rke2 >/dev/null || systemctl list-unit-files 'rke2-*' 2>/dev/null "
            "| grep -q rke2 && echo present || echo absent",
            check=False,
        ).strip() == "present"

    def _install_rke2(self, node: RKE2Node, role: str, version: Optional[str] = None) -> None:
        """Runs RKE2's get.rke2.io install script on `node` for the given
        `role` ("server" or "agent"), pinned to `version` if given (else
        latest stable). `role`/`version` are shell-quoted before going into
        the command line — `version` in particular is free-form and this
        line is executed via a real shell (ssh's remote-command handling,
        or local `bash -c`), so an unquoted value would let shell
        metacharacters (";", "|", "$()", ...) run as arbitrary commands."""
        env = f"INSTALL_RKE2_TYPE={shlex.quote(role)}"
        if version:
            env += f" INSTALL_RKE2_VERSION={shlex.quote(version)}"
        self._run(node, f"curl -sfL {INSTALL_SCRIPT_URL} | {env} sh -")

    def _write_cilium_chart_config(
        self,
        node: RKE2Node,
        disable_kube_proxy: bool = False,
        cilium_mtu: Optional[int] = None,
    ) -> None:
        """Stages the HelmChartConfig for RKE2's bundled Cilium chart in the
        auto-apply manifests directory, so it takes effect the moment
        `rke2-server` first starts on `node`.

        Carries whichever of the two settings this SDK models are in play:
        kube-proxy replacement, and the pod-network MTU.
        """
        # Per https://docs.rke2.io/networking/basic_network_options —
        # "localhost" works on every node (server or agent) because RKE2
        # runs a local API server supervisor proxy on port 6443 everywhere.
        self._run(node, "mkdir -p /var/lib/rancher/rke2/server/manifests")
        values = ""
        if disable_kube_proxy:
            values += (
                "    kubeProxyReplacement: true\n"
                '    k8sServiceHost: "localhost"\n'
                '    k8sServicePort: "6443"\n'
            )
        if cilium_mtu is not None:
            values += f"    MTU: {cilium_mtu}\n"
        manifest = (
            "apiVersion: helm.cattle.io/v1\n"
            "kind: HelmChartConfig\n"
            "metadata:\n"
            "  name: rke2-cilium\n"
            "  namespace: kube-system\n"
            "spec:\n"
            "  valuesContent: |-\n"
            f"{values}"
        )
        heredoc = (
            f"cat > {CILIUM_HELM_CHART_CONFIG_PATH} <<'RKE2_CILIUM_EOF'\n"
            f"{manifest}"
            f"RKE2_CILIUM_EOF"
        )
        self._run(node, heredoc)

    def _write_config(self, node: RKE2Node, contents: str) -> None:
        """Writes `contents` to `node`'s /etc/rancher/rke2/config.yaml via
        a quoted heredoc (over SSH/local-exec, so no second transport like
        scp is needed just to drop one small file)."""
        self._run(node, "mkdir -p /etc/rancher/rke2")
        # Write via a heredoc over SSH/local-exec rather than scp, so we
        # don't need a second transport just to drop one small file.
        heredoc = (
            f"cat > {CONFIG_PATH} <<'RKE2_CONFIG_EOF'\n"
            f"{contents}\n"
            f"RKE2_CONFIG_EOF"
        )
        self._run(node, heredoc)

    def _wait_for_service(self, node: RKE2Node, service: str) -> None:
        """Polls `systemctl is-active {service}` on `node` until it reports
        active, or raises `RKE2Error` after `self.poll_timeout` seconds."""
        deadline = time.time() + self.poll_timeout
        while time.time() < deadline:
            result = self._run(node, f"systemctl is-active {service}", check=False)
            if result.strip() == "active":
                return
            time.sleep(self.poll_interval)
        raise RKE2Error(
            f"Timed out waiting for {service} to become active on {node.address}"
        )

    def _wait_for_node_token(self, node: RKE2Node) -> None:
        """Polls the first server for its node-token file to appear
        (written once `rke2-server` has fully initialized), or raises
        `RKE2Error` after `self.poll_timeout` seconds."""
        deadline = time.time() + self.poll_timeout
        while time.time() < deadline:
            result = self._run(
                node, f"test -s {NODE_TOKEN_PATH} && echo ready", check=False
            )
            if result.strip() == "ready":
                return
            time.sleep(self.poll_interval)
        raise RKE2Error(
            f"Timed out waiting for node token on first server {node.address}"
        )

    def _fetch_kubeconfig(self, node: RKE2Node) -> str:
        """Reads rke2.yaml off `node` and rewrites its default
        `127.0.0.1` server address to `node.address`, so the returned
        kubeconfig works from the caller's machine and not just locally on
        the node (loopback addresses are left as-is, since the caller *is*
        the node in that case)."""
        raw = self._run(node, f"cat {KUBECONFIG_REMOTE_PATH}")
        # rke2.yaml points the server at 127.0.0.1 by default since it's
        # meant to be used locally on the server node — rewrite it so the
        # kubeconfig this method returns actually works from the caller's
        # machine. Skip the rewrite for loopback addresses: the caller is
        # the node in that case, so 127.0.0.1 is already correct.
        if node.address in ("127.0.0.1", "localhost"):
            return raw
        return raw.replace("127.0.0.1", node.address)

    def _write_kubeconfig_file(self, path: Optional[str], kubeconfig: str) -> None:
        """Writes `kubeconfig` to `path` (if given) with owner-only
        permissions — it carries cluster-admin credentials."""
        if path:
            with open(path, "w") as f:
                f.write(kubeconfig)
            # Holds cluster-admin credentials — don't leave it world/group-readable.
            os.chmod(path, 0o600)
