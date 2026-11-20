"""
SeaweedFS cluster backend.

Deploys and manages SeaweedFS clusters through the official SeaweedFS
Helm chart, shelling out to `helm` and `kubectl` the same way the
storage driver does. Every call is scoped to the spec's
`kubeconfig_path`: nothing here reads an ambient `$KUBECONFIG`, because a
stale default kubeconfig silently targets a cluster that may no longer
exist.

Unlike the MinIO Operator this replaces, there is no cluster-scoped
operator release to install first — one Helm release deploys
master+volume+filer+s3 together, so `create()` is a single
install-or-upgrade rather than an operator-then-tenant pair.

Credentials live in a Secret this backend applies with `kubectl apply`
(not one the chart renders), holding the `seaweedfs_s3_config` JSON the
S3 gateway reads identities from — referenced by name via
`s3.existingConfigSecret`. Owning that Secret ourselves is what makes
generate-if-unset / adopt-don't-regenerate possible: the chart's own
auto-generated secret has no equivalent read-back path.
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
import time
import yaml
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict

from ..kube import apply as kube_apply
from ..kube import require_cli, require_cluster, run_local
from ..core.seaweedfs import SeaweedFSCluster, parse_quantity
from ..netpolicy import apply_network_policy
from ..state.tracking import track_create, track_delete, track_update
from ..vault.client import default_vault_client
from .transport import NodeCommandError, NodeCommandMixin, NodePrerequisiteError

from multistack.helm import HelmRunner, HelmRelease


class SeaweedFSError(NodeCommandError):
    """Raised when a helm/kubectl call for a cluster fails."""


class SeaweedFSPrerequisiteError(NodePrerequisiteError):
    """Raised when the cluster can't support the SeaweedFS deployment as
    specified."""


class SeaweedFSTimeoutError(SeaweedFSError):
    """Raised when a cluster doesn't become ready within the timeout."""


class SeaweedFSStorageError(SeaweedFSError):
    """Raised when a PVC operation is invalid (e.g. shrinking) or fails."""


class SeaweedFSClusterInfo(BaseModel):
    """What `create()` returns: how to reach the cluster, and the
    credentials to do it with — which may have been generated."""
    model_config = ConfigDict(
        # A misspelled field name is a typo, not a value to keep.
        extra="forbid",
        # Re-checks on assignment, so mutating a spec into an invalid
        # state fails where it happens rather than at deploy time.
        validate_assignment=True,
        arbitrary_types_allowed=True,
    )

    name: str
    namespace: str
    endpoint: str
    access_key: str
    secret_key: str
    pod_names: List[str]
    status: str


class SeaweedFSBackend(NodeCommandMixin):
    """Creates and manages SeaweedFS clusters on an existing cluster."""

    error_cls = SeaweedFSError
    prerequisite_error_cls = SeaweedFSPrerequisiteError
    log_prefix = "seaweedfs"

    def __init__(
        self,
        command_timeout: int = 600,
        ready_timeout: int = 600,
        poll_interval: int = 10,
    ):
        self.command_timeout = command_timeout
        self.ready_timeout = ready_timeout
        self.poll_interval = poll_interval

    # -- prerequisites ------------------------------------------------------
    def check_prerequisites(self, cluster: SeaweedFSCluster) -> List[str]:
        """Verifies the cluster can host this deployment.

        Returns warnings; raises `SeaweedFSPrerequisiteError` on anything
        that would leave it permanently unschedulable.
        """
        cluster.validate()
        for cli in ("helm", "kubectl"):
            self._require_cli(cli)

        # SeaweedFSCluster.REQUIRES names "cluster"; this is where that
        # stops being a declaration. Without it a stale kubeconfig
        # surfaces as `x509: certificate signed by unknown authority`
        # from inside a helm call minutes later, which is a day we have
        # already spent.
        require_cluster(cluster.kubeconfig_path, capability="a SeaweedFS cluster")

        warnings: List[str] = []
        classes = self._storage_classes(cluster)

        if cluster.storage_class:
            if cluster.storage_class not in classes:
                raise SeaweedFSPrerequisiteError(
                    f"StorageClass '{cluster.storage_class}' does not exist on "
                    f"this cluster (found: {', '.join(classes) or 'none'}). "
                    "The volume servers' PVCs would stay Pending forever, "
                    "since nothing would provision them."
                )
        elif not classes:
            raise SeaweedFSPrerequisiteError(
                "This cluster has no StorageClass at all, so the volume "
                "servers' PVCs would never bind. A fresh RKE2 cluster ships "
                "no provisioner — install block storage first (see "
                "docs/storage.md), or name a class with storage_class=."
            )
        elif not self._default_storage_class(cluster):
            warnings.append(
                "no default StorageClass is marked on this cluster and "
                "storage_class is unset, so the PVCs may not bind. Name one "
                f"explicitly — available: {', '.join(classes)}."
            )

        if cluster.replication == "000":
            warnings.append(
                "replication='000' has no redundancy: losing a volume server "
                "loses the data it held. Fine for a lab, not for anything you "
                "can't re-create — raise it (e.g. '001' for one extra copy on "
                "another server in the same rack) once volume_replicas allows "
                "it."
            )
        return warnings

    # -- lifecycle ----------------------------------------------------------
    @track_create("objectstore", name_of=lambda cluster: cluster.name)
    def create(
        self,
        cluster: SeaweedFSCluster,
        wait_for_ready: bool = True,
    ) -> SeaweedFSClusterInfo:
        """Installs (or upgrades) the cluster's single Helm release, waits
        for it to come up, and returns how to reach it.

        Idempotent: an existing healthy release is upgraded in place. A
        release left in `failed` state is torn down first, since Helm
        refuses to upgrade over one.
        """
        warnings = self.check_prerequisites(cluster)
        for warning in warnings:
            print(f"[{self.log_prefix}] warning: {warning}")

        # An existing cluster's credentials must be read back, never
        # regenerated: `ensure_credentials()` would mint new ones, and
        # re-applying the config Secret would then rotate the live S3
        # gateway's credentials out from under everything already using
        # them.
        adopted = False
        if not cluster.access_key and self._find_release(cluster) is not None:
            existing = self._existing_credentials(cluster)
            if existing:
                cluster.set_credentials(*existing)
                adopted = True
                print(f"[{self.log_prefix}] reusing existing credentials for "
                      f"{cluster.name}")
            else:
                raise SeaweedFSError(
                    f"Cluster '{cluster.name}' already exists but its "
                    f"credentials could not be read from secret "
                    f"'{cluster.config_secret_name}'. Pass access_key/"
                    "secret_key explicitly — generating new ones here would "
                    "rotate the live cluster's credentials and break every "
                    "client already using them."
                )
        if not adopted:
            cluster.ensure_credentials()

        # Written before the chart is installed: s3.existingConfigSecret
        # names it, and the S3 gateway pod would otherwise come up
        # looking for a Secret that doesn't exist yet.
        self._apply_config_secret(cluster)

        self._deploy_cluster(cluster)

        if cluster.network_policy_allowed_ingress:
            self._apply_network_policy(cluster)

        pod_names: List[str] = []
        status = "deployed"
        if wait_for_ready:
            pod_names = self._wait_until_ready(cluster)
            status = "ready"

        info = SeaweedFSClusterInfo(
            name=cluster.name,
            namespace=cluster.namespace,
            endpoint=cluster.endpoint(),
            access_key=cluster.access_key or "",
            secret_key=cluster.secret_key or "",
            pod_names=pod_names,
            status=status,
        )
        print(f"[{self.log_prefix}] cluster {cluster.name} {status} at "
              f"{info.endpoint}")
        # Keyed by this cluster's own name, not a fixed string: more than
        # one SeaweedFS cluster can exist, adopted or freshly generated
        # either way -- a no-op when no Vault exists at all (see
        # multistack/vault/client.py).
        default_vault_client().store_secret(
            f"objectstore/{cluster.name}",
            {"access_key": info.access_key, "secret_key": info.secret_key},
            # Same name @track_create("objectstore", ...) above records
            # this cluster under, so StateManager.mark_secret_stored
            # writes onto the row create() itself already owns.
            deployment_name=cluster.name,
        )
        return info

    @track_delete(name_of=lambda cluster: cluster.name)
    def delete(self, cluster: SeaweedFSCluster, delete_pvcs: bool = False) -> None:
        """Uninstalls the cluster's Helm release.

        `helm uninstall` never removes PVCs, so the data outlives the
        release unless `delete_pvcs=True` — which is irreversible. The
        config Secret and NetworkPolicy are removed only then too: they
        aren't part of the release either, but re-creating the cluster
        soon after benefits from a credential Secret staying put (a plain
        `delete()` is often "tear down, redeploy shortly").
        """
        cluster.validate()
        self._require_cli("helm")

        if self._find_release(cluster) is not None:
            runner = self._helm_runner(cluster)
            runner.uninstall(
                cluster.resolved_release_name,
                namespace=cluster.namespace,
                missing_ok=True,
            )
            print(f"[{self.log_prefix}] uninstalled release "
                  f"{cluster.resolved_release_name}")

        # Not part of the Helm release -- applied standalone in create(),
        # so it survives `helm uninstall` unless removed here too.
        self._kubectl(
            cluster, "delete", "networkpolicy", self._network_policy_name(cluster),
            "-n", cluster.namespace, "--ignore-not-found",
        )

        if delete_pvcs:
            self._kubectl(
                cluster, "delete", "secret", cluster.config_secret_name,
                "-n", cluster.namespace, "--ignore-not-found",
            )
            removed = self.delete_pvcs(cluster)
            print(f"[{self.log_prefix}] deleted {len(removed)} PVC(s): {', '.join(removed)}")
        else:
            print(
                f"[{self.log_prefix}] PVCs left in place — pass delete_pvcs=True to "
                "remove the data too"
            )

    def endpoint(self, cluster: SeaweedFSCluster) -> str:
        """In-cluster S3 URL. Hand this to `Inference.s3_endpoint_url`."""
        return cluster.endpoint()

    # -- storage ------------------------------------------------------------
    def list_pvcs(self, cluster: SeaweedFSCluster) -> List[str]:
        """PVC names currently belonging to this cluster."""
        out = self._kubectl(
            cluster, "get", "pvc", "-n", cluster.namespace,
            "-l", cluster.label_selector,
            "-o", "jsonpath={range .items[*]}{.metadata.name}{'\\n'}{end}",
        )
        return [line for line in out.splitlines() if line.strip()]

    def pvc_sizes(self, cluster: SeaweedFSCluster) -> Dict[str, str]:
        """`{pvc_name: requested_size}` for this cluster's PVCs."""
        out = self._kubectl(
            cluster, "get", "pvc", "-n", cluster.namespace,
            "-l", cluster.label_selector,
            "-o", "jsonpath={range .items[*]}{.metadata.name} "
                  "{.spec.resources.requests.storage}{'\\n'}{end}",
        )
        sizes: Dict[str, str] = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2:
                sizes[parts[0]] = parts[1]
        return sizes

    # SeaweedFSStorageError from here is always pre-flight -- a shrink, or
    # no PVCs to resize. Both leave the cluster untouched.
    @track_update(name_of=lambda cluster: cluster.name,
                  refused=(SeaweedFSStorageError,))
    def resize_storage(self, cluster: SeaweedFSCluster, new_size: str) -> List[str]:
        """Grows this cluster's PVCs to `new_size`.

        Requires the StorageClass to have `allowVolumeExpansion: true`.
        Kubernetes cannot shrink a PVC, so a smaller size is rejected
        here before any call is made — the API server's own error for
        this is considerably less clear.
        """
        cluster.validate()
        sizes = self.pvc_sizes(cluster)
        if not sizes:
            raise SeaweedFSStorageError(
                f"No PVCs found for cluster '{cluster.name}' in namespace "
                f"'{cluster.namespace}'."
            )

        target = parse_quantity(new_size)
        for pvc_name, current in sizes.items():
            if current and parse_quantity(current) > target:
                raise SeaweedFSStorageError(
                    f"Cannot shrink PVC '{pvc_name}' from {current} to {new_size}; "
                    "Kubernetes does not support shrinking PVCs. To reduce size, "
                    "recreate the cluster with delete(delete_pvcs=True) then create()."
                )

        patch = json.dumps({"spec": {"resources": {"requests": {"storage": new_size}}}})
        for pvc_name in sizes:
            self._kubectl(
                cluster, "patch", "pvc", pvc_name, "-n", cluster.namespace,
                "--type", "merge", "-p", patch,
            )
        # Keep the spec consistent, so a later create() doesn't try to
        # set the dataDir back to the old size.
        cluster.volume_size = new_size
        return sorted(sizes)

    @track_update(name_of=lambda cluster: cluster.name)
    def delete_pvcs(self, cluster: SeaweedFSCluster) -> List[str]:
        """Deletes this cluster's PVCs. Irreversible — the data goes with them."""
        cluster.validate()
        names = self.list_pvcs(cluster)
        for name in names:
            self._kubectl(
                cluster, "delete", "pvc", name, "-n", cluster.namespace,
                "--ignore-not-found",
            )
        return names

    # -- internals ----------------------------------------------------------
    def _apply_config_secret(self, cluster: SeaweedFSCluster) -> None:
        """Applies the namespace (if needed) and the S3 credentials Secret
        the chart's `s3.existingConfigSecret` will point at.

        Namespace first, in the same call: the chart only creates one at
        install time, which is too late — this Secret has to exist before
        the S3 gateway pod is even scheduled, so it's applied ahead of
        the Helm release rather than left to `create_namespace=True`.
        """
        objects = [
            {"apiVersion": "v1", "kind": "Namespace",
             "metadata": {"name": cluster.namespace}},
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "type": "Opaque",
                "metadata": {
                    "name": cluster.config_secret_name,
                    "namespace": cluster.namespace,
                },
                "stringData": {"seaweedfs_s3_config": cluster.s3_config_json()},
            },
        ]
        kube_apply(cluster.kubeconfig_path, objects, error_cls=SeaweedFSError)

    def _deploy_cluster(self, cluster: SeaweedFSCluster) -> None:
        """Installs or upgrades the cluster's single Helm release."""
        runner = self._helm_runner(cluster)
        existing = self._find_release(cluster)
        if existing is not None and existing.status.value == "failed":
            # Helm refuses to upgrade over a failed release, so clear it
            # rather than leaving the caller with an unhelpful error.
            print(f"[{self.log_prefix}] clearing failed release "
                  f"{cluster.resolved_release_name}")
            runner.uninstall(
                cluster.resolved_release_name,
                namespace=cluster.namespace,
            )

        values_path = self._write_values(cluster)
        try:
            values = self._load_values(values_path)
            print(f"[{self.log_prefix}] deploying {cluster.resolved_release_name} "
                  f"(master x{cluster.master_replicas}, filer x"
                  f"{cluster.filer_replicas}, volume x{cluster.volume_replicas} "
                  f"@ {cluster.volume_size} each, replication="
                  f"{cluster.replication})")
            runner.install_or_upgrade(
                cluster.resolved_release_name,
                chart=cluster.chart_name,
                namespace=cluster.namespace,
                values=values,
            )
        finally:
            os.unlink(values_path)

    def _load_values(self, values_path: str) -> Dict[str, Any]:
        with open(values_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def _network_policy_name(self, cluster: SeaweedFSCluster) -> str:
        return f"{cluster.name}-allow-clients"

    def _apply_network_policy(self, cluster: SeaweedFSCluster) -> None:
        """Restricts the cluster's S3 port to
        `network_policy_allowed_ingress`.

        Applied standalone via `kubectl apply`, not through the chart --
        seaweedfs/seaweedfs is a third-party chart and isn't ours to add
        templates to. Unlike MinIO's tenant chart, the S3 Service's
        `targetPort` is simply `s3.port` (`templates/s3/s3-service.yaml`:
        `targetPort: {{ .Values.s3.port }}`) -- no fixed container port
        that diverges from the advertised one, so the policy's port is
        just `cluster.s3_port`.
        """
        port = cluster.s3_port
        apply_network_policy(
            cluster.kubeconfig_path,
            self._network_policy_name(cluster),
            cluster.namespace,
            {
                "app.kubernetes.io/instance": cluster.resolved_release_name,
                "app.kubernetes.io/component": "s3",
            },
            allowed_ingress=cluster.network_policy_allowed_ingress,
            ports=[port],
            error_cls=SeaweedFSError,
        )
        print(f"[{self.log_prefix}] restricted {cluster.name} ingress to "
              f"{len(cluster.network_policy_allowed_ingress)} selector(s) on "
              f"port {port}")

    def _write_values(self, cluster: SeaweedFSCluster) -> str:
        """Writes the release values to a private temp file.

        JSON, because it is a subset of YAML that Helm parses happily and
        needs no third-party serializer. 0600 to match the convention
        every chart-backed backend in this SDK follows for its values
        file -- `helm_values()` itself carries no credentials (those live
        in the config Secret applied separately), but nothing else this
        SDK writes for Helm goes through `--set`, and there is no reason
        for this one to be the exception.
        """
        fd, path = tempfile.mkstemp(
            prefix=f"{cluster.name}-values-", suffix=".yaml"
        )
        with os.fdopen(fd, "w") as handle:
            json.dump(cluster.helm_values(), handle, indent=2)
        os.chmod(path, 0o600)
        return path

    def _existing_credentials(self, cluster: SeaweedFSCluster):
        """Read a live cluster's S3 credentials back from the config
        Secret this backend manages. Returns None when the secret is
        absent, unreadable, or doesn't carry a usable identity."""
        raw = self._kubectl(
            cluster, "get", "secret", cluster.config_secret_name,
            "-n", cluster.namespace, "-o", "json", check=False,
        )
        if not raw.strip():
            return None
        try:
            data = json.loads(raw).get("data", {})
        except json.JSONDecodeError:
            return None

        encoded = data.get("seaweedfs_s3_config")
        if not encoded:
            return None
        try:
            config = json.loads(base64.b64decode(encoded).decode("utf-8"))
            credential = config["identities"][0]["credentials"][0]
            access_key, secret_key = credential["accessKey"], credential["secretKey"]
        except (ValueError, KeyError, IndexError, TypeError):
            return None
        return (access_key, secret_key) if access_key and secret_key else None

    def _wait_until_ready(self, cluster: SeaweedFSCluster) -> List[str]:
        """Polls until every cluster pod is Running.

        Reports what is blocking rather than only timing out: an unbound
        PVC never resolves on its own, so waiting out the full timeout to
        say "not ready" wastes minutes and explains nothing.
        """
        deadline = time.time() + self.ready_timeout
        last = ""
        while True:
            pods = self._pod_phases(cluster)
            if pods and all(phase == "Running" for _, phase in pods):
                names = [name for name, _ in pods]
                print(f"[{self.log_prefix}] {len(names)} pod(s) running")
                return names

            summary = " ".join(f"{n}={p}" for n, p in pods) or "no pods yet"
            if summary != last:
                print(f"[{self.log_prefix}]   {summary}")
                last = summary

            if time.time() >= deadline:
                raise SeaweedFSTimeoutError(
                    f"Cluster '{cluster.name}' was not ready within "
                    f"{self.ready_timeout}s.\n" + self._diagnose(cluster)
                )
            time.sleep(self.poll_interval)

    def _diagnose(self, cluster: SeaweedFSCluster) -> str:
        """Explains why pods aren't Running — usually an unbound PVC."""
        lines: List[str] = []
        pending = self._kubectl(
            cluster, "get", "pvc", "-n", cluster.namespace,
            "-o", "jsonpath={range .items[?(@.status.phase!='Bound')]}"
                  "{.metadata.name} {.status.phase} "
                  "sc={.spec.storageClassName}{'\\n'}{end}",
            check=False,
        ).strip()
        if pending:
            lines.append("Unbound PVCs (nothing will provision these):")
            lines.extend(f"  {line}" for line in pending.splitlines())
            lines.append(
                "  A PVC with no matching provisioner never binds. Check "
                "`kubectl get storageclass`."
            )

        events = self._kubectl(
            cluster, "get", "events", "-n", cluster.namespace,
            "--field-selector", "type=Warning",
            "-o", "jsonpath={range .items[*]}{.message}{'\\n'}{end}",
            check=False,
        ).strip()
        if events:
            lines.append("Recent warnings:")
            lines.extend(f"  {line}" for line in events.splitlines()[-5:])
        return "\n".join(lines) or "No PVC or event detail available."

    def _pod_phases(self, cluster: SeaweedFSCluster) -> List[tuple]:
        out = self._kubectl(
            cluster, "get", "pods", "-n", cluster.namespace,
            "-l", cluster.label_selector,
            "-o", "jsonpath={range .items[*]}{.metadata.name} {.status.phase}{'\\n'}{end}",
            check=False,
        )
        phases = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2:
                phases.append((parts[0], parts[1]))
        return phases

    def _storage_classes(self, cluster: SeaweedFSCluster) -> List[str]:
        out = self._kubectl(
            cluster, "get", "storageclass",
            "-o", "jsonpath={range .items[*]}{.metadata.name}{'\\n'}{end}",
            check=False,
        )
        return [line for line in out.splitlines() if line.strip()]

    def _default_storage_class(self, cluster: SeaweedFSCluster) -> Optional[str]:
        out = self._kubectl(
            cluster, "get", "storageclass",
            "-o", "jsonpath={range .items[?(@.metadata.annotations."
                  "storageclass\\.kubernetes\\.io/is-default-class=='true')]}"
                  "{.metadata.name}{'\\n'}{end}",
            check=False,
        ).strip()
        return out.splitlines()[0] if out else None

    def _find_release(self, cluster: SeaweedFSCluster) -> Optional[HelmRelease]:
        """The Helm release for this cluster, or None."""
        runner = self._helm_runner(cluster)
        return runner.get_release(
            cluster.resolved_release_name,
            namespace=cluster.namespace,
        )

    def _helm_runner(self, cluster: SeaweedFSCluster) -> HelmRunner:
        return HelmRunner(
            cluster.kubeconfig_path,
        )

    def _kubectl(self, cluster: SeaweedFSCluster, *args: str, check: bool = True) -> str:
        return self._local(
            ["kubectl", "--kubeconfig", cluster.kubeconfig_path, *args], check=check
        )

    def _local(self, argv: List[str], check: bool = True) -> str:
        """Runs a local CLI. Mechanics shared (see multistack/kube.py), so
        this inherits stdin being closed and a timeout raised as
        SeaweedFSError rather than a bare subprocess.TimeoutExpired."""
        return run_local(
            argv, check=check, timeout=self.command_timeout, error_cls=SeaweedFSError
        )

    def _require_cli(self, name: str) -> None:
        # Nothing is executed: a presence check that ran `kubectl version`
        # could contact the API server and hang on an unreachable cluster.
        require_cli(
            name, error_cls=SeaweedFSPrerequisiteError,
            purpose="this backend drives it directly",
        )
