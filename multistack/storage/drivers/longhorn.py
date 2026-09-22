"""
Longhorn storage backend.

Two halves, because Longhorn needs both:

  * **Node preparation** — Longhorn attaches volumes through `iscsiadm` on
    the host, so every node needs open-iscsi installed with `iscsid`
    running, plus an NFSv4 client for RWX volumes. These are checked (and
    optionally installed) over SSH, reusing the same transport the RKE2
    backend uses.
  * **Cluster installation** — the chart itself goes on via `helm`, against
    the kubeconfig named in the spec.

Requirements on the machine running this: `helm` and `kubectl` on PATH, plus
`ssh` and passwordless key-based SSH to the nodes for the preparation half.
Unlike ambient-kubeconfig tooling, every cluster-side command here is
explicitly scoped with `--kubeconfig`, so it can only ever act on the
cluster the spec names.

Prerequisites per https://longhorn.io/docs/ (checked 2026-09-03).
"""
from __future__ import annotations
from typing import List, Optional

from ..spec import Storage
from ..base import StorageError, StoragePrerequisiteError
from ...backends.transport import NodeCommandMixin
from ...kube import require_cli, run_local
from ...helm import HelmRunner

# Binaries Longhorn's node components shell out to.
REQUIRED_BINARIES = ["bash", "curl", "findmnt", "grep", "awk", "blkid", "lsblk"]
# Longhorn overrides its kubelet-path detection from this env var, which is
# only needed when the distribution puts kubelet somewhere non-standard.
# RKE2 uses the standard path unless kubelet-arg root-dir was overridden.
STANDARD_KUBELET_DIR = "/var/lib/kubelet"
MULTIPATH_KB_URL = "https://longhorn.io/kb/troubleshooting-volume-with-multipath/"


class LonghornPrerequisiteError(StoragePrerequisiteError):
    """Raised when a node doesn't meet a hard Longhorn requirement."""


class LonghornError(StorageError):
    """Raised for any other Longhorn install/teardown failure."""


class LonghornDriver(NodeCommandMixin):
    @staticmethod
    def _deep_merge(base: dict, overrides: dict) -> dict:
        """Recursively merge Helm value overrides into the base values."""
        for key, value in overrides.items():
            if (
                key in base
                and isinstance(base[key], dict)
                and isinstance(value, dict)
            ):
                LonghornDriver._deep_merge(base[key], value)
            else:
                base[key] = value

        return base

    @staticmethod
    def _helm_values(storage: Storage) -> dict:
        """Maps the generic Storage spec onto Longhorn's chart values.

        This is the translation layer that lets `Storage` stay
        implementation-neutral: `replica_count` means the same thing to
        every driver, but only this one knows it is spelled
        `defaultSettings.defaultReplicaCount`. `extra_values` is merged
        last so it can override anything modelled here.
        """
        values = {
            "persistence": {
                "defaultClass": storage.default_storage_class,
                "defaultClassReplicaCount": storage.replica_count,
            },
            "defaultSettings": {
                "defaultReplicaCount": storage.replica_count,
                "defaultDataPath": storage.options.data_path,
            },
            "csi": {
                # Skips Longhorn's kubelet-cmdline auto-detection, which is a
                # single point of failure for the whole CSI driver.
                "kubeletRootDir": storage.options.kubelet_root_dir,
            },
        }

        return LonghornDriver._deep_merge(values, storage.extra_values)

    """Prepares nodes for Longhorn and installs it into a cluster.

    Command execution, probes, and parallel fan-out come from
    `NodeCommandMixin`, shared with the RKE2 backend.
    """

    error_cls = LonghornError
    prerequisite_error_cls = LonghornPrerequisiteError
    log_prefix = "longhorn"

    def __init__(
        self,
        ssh_timeout: int = 30,
        command_timeout: int = 900,
        max_parallel: int = 10,
        install_timeout: int = 900,
    ):
        """
        `ssh_timeout`/`command_timeout`/`max_parallel`: node-command
        transport settings, as on `RKE2Backend`.
        `install_timeout`: how long to let `helm --wait` block for Longhorn's
        pods to come up. Longhorn pulls several images per node, so a cold
        cluster legitimately takes minutes.
        """
        self.ssh_timeout = ssh_timeout
        self.command_timeout = command_timeout
        self.max_parallel = max_parallel
        self.install_timeout = install_timeout

    # -- node preparation --------------------------------------------------
    def check_prerequisites(
        self, storage: Storage, nodes: Optional[List] = None
    ) -> List[str]:
        """
        Verifies every node in `nodes` can actually run Longhorn, and
        returns warning strings for soft problems. Raises
        `LonghornPrerequisiteError` for hard ones — a missing iscsiadm or a
        stopped `iscsid` means volumes silently fail to attach later, which
        is far more painful to debug than failing here.

        `nodes` is optional, as the Protocol and docs say: without it this
        checks only what can be known from the spec, which is what a
        caller that has no SSH access to the nodes gets. The per-node
        checks are the valuable half, so passing them is strongly advised.
        """
        warnings: List[str] = []

        if not nodes:
            warnings.append(
                "no nodes were given, so open-iscsi, iscsid, the NFSv4 client "
                "and the required binaries were not checked. Longhorn's node "
                "components crash-loop without them and volumes then fail to "
                "attach with no obvious cause — pass nodes= to check."
            )

        if nodes and storage.replica_count > len(nodes):
            warnings.append(
                f"replica_count={storage.replica_count} exceeds the {len(nodes)} "
                "node(s) given — Longhorn can't place more replicas than it has "
                "nodes, so volumes will stay Degraded. Reduce replica_count or "
                "add nodes."
            )

        per_node = self._run_parallel(
            nodes,
            lambda node: self._check_node(node, storage),
            label="prerequisite check",
        )
        for node_warnings in per_node.values():
            warnings.extend(node_warnings)
        return warnings

    def _check_cluster_coverage(self, storage: Storage, nodes: List) -> List[str]:
        """
        Warns about cluster nodes that `nodes` doesn't cover.

        Longhorn's node components are a DaemonSet — they run on every
        schedulable node in the cluster, not just the ones handed to this
        backend. So preparing and checking a subset is a false green: the
        uncovered nodes still get a `longhorn-manager`, and any that lack
        open-iscsi crash-loop, degrading the whole storage layer while the
        prerequisite check reported success.

        Compares by InternalIP, which equals the declared address when the
        cluster was built with `pin_node_ip` (the default).
        """
        given = {node.address for node in nodes}
        try:
            raw = self._kubectl(
                storage,
                "get", "nodes",
                "-o", "jsonpath={range .items[*]}{.metadata.name}"
                "={.status.addresses[?(@.type=='InternalIP')].address}{'\\n'}{end}",
            )
        except LonghornError:
            # Not worth failing the install over a check that couldn't run.
            return ["couldn't list cluster nodes to verify prerequisite coverage"]

        uncovered = []
        for line in raw.splitlines():
            if "=" not in line:
                continue
            name, _, address = line.strip().partition("=")
            if address and address not in given:
                uncovered.append(f"{name} ({address})")

        if not uncovered:
            return []
        return [
            f"{len(uncovered)} cluster node(s) not in the list passed here: "
            f"{', '.join(uncovered)}. Longhorn's node components are a DaemonSet "
            "and will run there anyway — without open-iscsi they crash-loop and "
            "Longhorn runs degraded. Pass every cluster node, or exclude these "
            "from Longhorn with a node selector/taint."
        ]

    def _check_node(self, node, storage: Optional[Storage]) -> List[str]:
        """Per-node prerequisite checks. Hard failures raise; everything
        recoverable comes back as a warning string."""
        warnings: List[str] = []

        # First — installing packages and managing iscsid both need it.
        self._check_passwordless_sudo(node)

        if self._probe(
            node, "command -v iscsiadm >/dev/null && echo yes || echo no", "iscsiadm"
        ) != "yes":
            raise LonghornPrerequisiteError(
                f"{node.address}: open-iscsi is not installed. Longhorn attaches "
                "volumes through iscsiadm on the host, so this is required. "
                "Install it with StorageBackend.install_prerequisites(storage, nodes), "
                "or by hand (`apt-get install -y open-iscsi` / "
                "`dnf install -y iscsi-initiator-utils`)."
            )

        if self._probe(
            node, "systemctl is-active iscsid || true", "iscsid state"
        ) != "active":
            raise LonghornPrerequisiteError(
                f"{node.address}: the iscsid service is installed but not "
                "running, so volume attachment will fail. Start it with "
                "`sudo systemctl enable --now iscsid`, or call "
                "StorageBackend.install_prerequisites(storage, nodes)."
            )

        missing = [
            b
            for b in REQUIRED_BINARIES
            if self._probe(
                node, f"command -v {b} >/dev/null && echo yes || echo no", f"{b} presence"
            )
            != "yes"
        ]
        if missing:
            raise LonghornPrerequisiteError(
                f"{node.address}: Longhorn's node components need these "
                f"binaries, which are missing: {', '.join(missing)}"
            )

        has_nfs = self._probe(
            node,
            "command -v mount.nfs4 >/dev/null && echo yes || echo no",
            "NFSv4 client",
        ) == "yes"
        if not has_nfs:
            if storage is not None and storage.options.enable_rwx:
                raise LonghornPrerequisiteError(
                    f"{node.address}: no NFSv4 client, which RWX (ReadWriteMany) "
                    "volumes are served over. Install it "
                    "(`apt-get install -y nfs-common` / `dnf install -y nfs-utils`) "
                    "or set enable_rwx=False on the spec if you only need RWO."
                )
            warnings.append(
                f"{node.address}: no NFSv4 client — RWX volumes and NFS backup "
                "targets won't work (RWO volumes are unaffected)"
            )

        # Soft, but a well-known cause of "MountVolume.SetUp failed": if
        # multipathd is running it can claim Longhorn's block devices unless
        # they're blacklisted. Not auto-fixed here — editing multipath.conf
        # on a live node is too blunt an action to take implicitly.
        if self._probe(
            node, "systemctl is-active multipathd || true", "multipathd state"
        ) == "active":
            warnings.append(
                f"{node.address}: multipathd is running and can claim Longhorn's "
                "devices, causing MountVolume.SetUp failures. Blacklist Longhorn's "
                f"devices in /etc/multipath.conf — see {MULTIPATH_KB_URL} — or stop "
                "multipathd if nothing else needs it."
            )

        kubelet_dir_present = self._probe(
            node,
            f"test -d {STANDARD_KUBELET_DIR} && echo yes || echo no",
            "kubelet directory",
        )
        if kubelet_dir_present != "yes":
            warnings.append(
                f"{node.address}: {STANDARD_KUBELET_DIR} not found, so this node "
                "may use a non-standard kubelet root directory. Longhorn's CSI "
                "plugin needs KUBELET_ROOT_DIR set to match — pass it via "
                "extra_values if volume mounts fail."
            )

        return warnings

    def install_prerequisites(self, nodes: List) -> None:
        """
        Installs open-iscsi and the NFSv4 client on every node and enables
        `iscsid`. Mutates the nodes, so it's a separate opt-in call rather
        than something `create()` does implicitly.

        Handles apt- and dnf/yum-based distributions; anything else raises,
        rather than guessing at a package manager.
        """
        self._run_parallel(nodes, self._install_node_prerequisites, label="prereq install")

    def _install_node_prerequisites(self, node) -> None:
        """Installs Longhorn's node packages on one node, picking the
        command set from whichever package manager the node actually has."""
        if self._probe(
            node, "command -v apt-get >/dev/null && echo yes || echo no", "apt-get"
        ) == "yes":
            steps = [
                "apt-get update -qq",
                "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq open-iscsi nfs-common",
            ]
        elif self._probe(
            node, "command -v dnf >/dev/null && echo yes || echo no", "dnf"
        ) == "yes":
            steps = ["dnf install -y -q iscsi-initiator-utils nfs-utils"]
        elif self._probe(
            node, "command -v yum >/dev/null && echo yes || echo no", "yum"
        ) == "yes":
            steps = ["yum install -y -q iscsi-initiator-utils nfs-utils"]
        else:
            raise LonghornPrerequisiteError(
                f"{node.address}: no apt-get, dnf or yum found — install "
                "open-iscsi and an NFSv4 client manually, then re-run "
                "check_prerequisites()."
            )

        # iscsid must be enabled *and* started: installing the package
        # doesn't leave it running on every distribution (Ubuntu ships it
        # inactive), which is exactly the state that makes volumes fail to
        # attach with no obvious cause.
        steps.append("systemctl enable --now iscsid")

        for step in steps:
            self._run(node, step)

    # -- cluster installation ----------------------------------------------
    def create(self, storage: Storage, nodes: Optional[List] = None) -> str:
        """
        Installs (or upgrades) Longhorn in the cluster `storage` names and
        returns the name of the StorageClass it created.

        Idempotent: uses `helm upgrade --install`, so running it against an
        existing release reconciles it to the spec rather than failing.

        If `nodes` is given, their prerequisites are checked first — do
        pass them, since the alternative is discovering a stopped `iscsid`
        later as an unexplained volume-attachment hang.
        """
        storage.validate()

        self._require_cli("helm")
        self._require_cli("kubectl")

        if nodes:
            print(f"[longhorn] checking prerequisites on {len(nodes)} node(s)")
            for w in self.check_prerequisites(storage, nodes):
                print(f"[longhorn] warning: {w}")
            for w in self._check_cluster_coverage(storage, nodes):
                print(f"[longhorn] warning: {w}")

        runner = self._helm_runner(storage)

        print(
            f"[longhorn] installing {storage.options.release_name} into {storage.resolved_namespace} "
            f"(replicas={storage.replica_count}, may take several minutes)"
        )
        runner.install_or_upgrade(
            storage.options.release_name,
            chart=storage.options.chart,
            namespace=storage.resolved_namespace,
            chart_version=storage.chart_version,
            values=self._helm_values(storage),
            atomic=True,
            wait=True,
            create_namespace=True,
        )

        storage_class = self._verify_storage_class(storage)
        print(f"[longhorn] ready — StorageClass '{storage_class}' available")
        return storage_class

    def delete(self, storage: Storage) -> None:
        """
        Uninstalls the Longhorn release.

        Deliberately does not delete Longhorn's CRDs or volume data:
        removing them destroys every persistent volume in the cluster, which
        is not something to do as a side effect of a teardown call. Remove
        them by hand once you're certain nothing needs the data.
        """
        storage.validate()
        self._require_cli("helm")

        self._confirm_deletion(storage)

        print(f"[longhorn] uninstalling release '{storage.options.release_name}'")
        runner = self._helm_runner(storage)

        runner.uninstall(
            storage.options.release_name,
            namespace=storage.resolved_namespace,
            wait=True,
        )

        print(
            "[longhorn] release removed. Longhorn's CRDs and volume data are "
            "intentionally left in place — delete them manually if you mean to "
            "discard every volume."
        )

    def _confirm_deletion(self, storage: Storage) -> None:
        """Sets Longhorn's `deleting-confirmation-flag` before uninstalling.

        Longhorn ships a guard against accidental teardown: its uninstaller
        job refuses to run unless this setting is true. Without it
        `helm uninstall` removes the release while the uninstaller never
        completes, and the namespace sits Terminating behind CRDs and
        finalizers that then have to be unstuck by hand.

        Best-effort on purpose. If the setting isn't there — an older
        Longhorn, or a release that never finished installing — that is not
        a reason to refuse to uninstall. The warning says what to expect.
        """
        try:
            self._kubectl(
                storage,
                "-n", storage.resolved_namespace,
                "patch", "settings.longhorn.io", "deleting-confirmation-flag",
                "--type=merge", "-p", '{"value":"true"}',
            )
            print("[longhorn] set deleting-confirmation-flag")
        except LonghornError as exc:
            print(
                "[longhorn] warning: could not set deleting-confirmation-flag "
                f"({str(exc).splitlines()[0][:120]}). Uninstall will proceed, but "
                "Longhorn's uninstaller may not run and the namespace can be "
                "left Terminating."
            )

    def _verify_storage_class(self, storage: Storage) -> str:
        """Confirms the StorageClass actually exists after install, and
        returns its name.

        Worth checking explicitly: a release that reports Deployed but left
        no StorageClass produces PVCs that sit Pending forever, and the
        failure then looks like it belongs to whatever component tried to
        use the storage.
        """
        out = self._kubectl(
            storage,
            "get", "storageclass",
            "-o", "jsonpath={range .items[*]}{.metadata.name}{'\\n'}{end}",
        )
        classes = [line.strip() for line in out.splitlines() if line.strip()]
        if storage.options.release_name in classes:
            return storage.options.release_name
        raise LonghornError(
            f"Longhorn installed but no '{storage.options.release_name}' StorageClass "
            f"exists (found: {classes or 'none'}). PVCs would stay Pending. "
            "Check `kubectl -n "
            f"{storage.resolved_namespace} get pods --kubeconfig {storage.kubeconfig_path}`."
        )

    # -- local CLI plumbing -------------------------------------------------
    def _require_cli(self, name: str) -> None:
        """Fails early and by name if a required CLI isn't on PATH, rather
        than surfacing a raw FileNotFoundError from subprocess."""
        require_cli(
            name, error_cls=LonghornError,
            purpose="the Longhorn driver drives it directly. Install it and re-run",
        )

    def _helm_runner(self, storage: Storage) -> HelmRunner:
        """Create a Helm runner scoped to the target cluster."""
        return HelmRunner(
            storage.kubeconfig_path,
            timeout=f"{self.install_timeout}s",
        )

    def _kubectl(self, storage: Storage, *args: str) -> str:
        """Runs `kubectl` against the cluster this spec names — always with
        an explicit `--kubeconfig`, never ambient resolution.

        Goes through `_local` rather than calling the shared helper
        directly, so this driver keeps one seam for every local command.
        A test asserts on the argv that passes through it, which is how
        "no cluster command is ever unscoped" is actually enforced.
        """
        return self._local(
            ["kubectl", "--kubeconfig", storage.kubeconfig_path, *args]
        )

    @property
    def _local_timeout(self) -> int:
        """Wall-clock cap for local CLI calls. Sized for a chart install,
        which is the long one; a quick `kubectl get` just finishes first."""
        return self.install_timeout + 60

    def _local(self, argv: List[str]) -> str:
        """Runs a local CLI with stdin closed and a wall-clock cap, for the
        same reasons `_run` does on nodes. The mechanics are shared (see
        multistack/kube.py); the error type is ours."""
        return run_local(argv, timeout=self._local_timeout, error_cls=LonghornError)
