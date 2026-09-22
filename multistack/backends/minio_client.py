"""
MinIO tenant backend.

Deploys and manages MinIO tenants through the MinIO Operator Helm charts,
shelling out to `helm` and `kubectl` the same way the storage driver does.
Every call is scoped to the spec's `kubeconfig_path`: nothing here reads
an ambient `$KUBECONFIG`, because a stale default kubeconfig silently
targets a cluster that may no longer exist.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
import time
import yaml
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..kube import require_cli, require_cluster, run_local
from ..core.minio import MinIOTenant, parse_quantity
from ..netpolicy import apply_network_policy
from ..state.tracking import track_create, track_delete, track_update
from .transport import NodeCommandError, NodeCommandMixin, NodePrerequisiteError

from multistack.helm import HelmRunner, HelmRelease


class MinIOError(NodeCommandError):
    """Raised when a helm/kubectl call for a tenant fails."""


class MinIOPrerequisiteError(NodePrerequisiteError):
    """Raised when the cluster can't support the tenant as specified."""


class MinIOTimeoutError(MinIOError):
    """Raised when a tenant doesn't become ready within the timeout."""


class MinIOStorageError(MinIOError):
    """Raised when a PVC operation is invalid (e.g. shrinking) or fails."""


class MinIOTenantInfo(BaseModel):
    """What `create()` returns: how to reach the tenant, and the
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
    root_user: str
    root_password: str
    pod_names: List[str]
    status: str


class MinIOBackend(NodeCommandMixin):
    """Creates and manages MinIO tenants on an existing cluster."""

    error_cls = MinIOError
    prerequisite_error_cls = MinIOPrerequisiteError
    log_prefix = "minio"

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
    def check_prerequisites(self, tenant: MinIOTenant) -> List[str]:
        """Verifies the cluster can host this tenant.

        Returns warnings; raises `MinIOPrerequisiteError` on anything that
        would leave the tenant permanently unschedulable.
        """
        tenant.validate()
        for cli in ("helm", "kubectl"):
            self._require_cli(cli)

        # MinIOTenant.REQUIRES names "cluster"; this is where that stops
        # being a declaration. Without it a stale kubeconfig surfaces as
        # `x509: certificate signed by unknown authority` from inside a
        # helm call minutes later, which is a day we have already spent.
        require_cluster(tenant.kubeconfig_path, capability="a MinIO tenant")

        warnings: List[str] = []
        classes = self._storage_classes(tenant)

        if tenant.storage_class:
            if tenant.storage_class not in classes:
                raise MinIOPrerequisiteError(
                    f"StorageClass '{tenant.storage_class}' does not exist on this "
                    f"cluster (found: {', '.join(classes) or 'none'}). The tenant's "
                    "PVCs would stay Pending forever, since nothing would provision "
                    "them."
                )
        elif not classes:
            raise MinIOPrerequisiteError(
                "This cluster has no StorageClass at all, so the tenant's PVCs "
                "would never bind. A fresh RKE2 cluster ships no provisioner — "
                "install block storage first (see examples/storage/install.py), "
                "or name a class with storage_class=."
            )
        elif not self._default_storage_class(tenant):
            warnings.append(
                "no default StorageClass is marked on this cluster and "
                "storage_class is unset, so the PVCs may not bind. Name one "
                f"explicitly — available: {', '.join(classes)}."
            )

        if tenant.mode == "standalone":
            warnings.append(
                "mode='standalone' has no redundancy: losing the node loses the "
                "data. Fine for a lab, not for anything you can't re-create."
            )
        return warnings

    # -- lifecycle ----------------------------------------------------------
    @track_create("objectstore", name_of=lambda tenant: tenant.name)
    def create(
        self,
        tenant: MinIOTenant,
        wait_for_ready: bool = True,
        install_operator: bool = True,
    ) -> MinIOTenantInfo:
        """Installs the operator (if needed) and the tenant, waits for it
        to come up, and returns how to reach it.

        Idempotent: an existing healthy release is upgraded in place. A
        release left in `failed` state is torn down first, since Helm
        refuses to upgrade over one.
        """
        warnings = self.check_prerequisites(tenant)
        for warning in warnings:
            print(f"[{self.log_prefix}] warning: {warning}")

        # An existing tenant's credentials must be read back, never
        # regenerated: `ensure_credentials()` would mint new ones, and the
        # Helm upgrade would then rotate the live tenant's root
        # credentials out from under everything already using them.
        adopted = False
        if not tenant.root_user and self._find_release(tenant) is not None:
            existing = self._existing_credentials(tenant)
            if existing:
                tenant.set_credentials(*existing)
                adopted = True
                print(f"[{self.log_prefix}] reusing existing credentials for "
                      f"{tenant.name}")
            else:
                raise MinIOError(
                    f"Tenant '{tenant.name}' already exists but its credentials "
                    f"could not be read from secret '{tenant.config_secret_name}'. "
                    "Pass root_user/root_password explicitly — generating new ones "
                    "here would rotate the live tenant's credentials and break "
                    "every client already using them."
                )
        if not adopted:
            tenant.ensure_credentials()

        # Unconditionally, and before either chart is referenced. Doing
        # this only when installing the operator means the second tenant
        # on a cluster — where the operator is already present, since it
        # is cluster-scoped — fails with `repo minio not found`.

        if install_operator:
            self._install_operator(tenant)

        self._deploy_tenant(tenant)

        if tenant.network_policy_allowed_ingress:
            self._apply_network_policy(tenant)

        pod_names: List[str] = []
        status = "deployed"
        if wait_for_ready:
            pod_names = self._wait_until_ready(tenant)
            status = "ready"

        info = MinIOTenantInfo(
            name=tenant.name,
            namespace=tenant.namespace,
            endpoint=tenant.endpoint(),
            root_user=tenant.root_user or "",
            root_password=tenant.root_password or "",
            pod_names=pod_names,
            status=status,
        )
        print(f"[{self.log_prefix}] tenant {tenant.name} {status} at {info.endpoint}")
        return info

    @track_delete(name_of=lambda tenant: tenant.name)
    def delete(self, tenant: MinIOTenant, delete_pvcs: bool = False) -> None:
        """Uninstalls the tenant's Helm release.

        `helm uninstall` never removes PVCs, so the data outlives the
        release unless `delete_pvcs=True` — which is irreversible.
        """
        tenant.validate()
        self._require_cli("helm")

        if self._find_release(tenant) is not None:
            runner = self._helm_runner(tenant)
            runner.uninstall(
                tenant.name,
                namespace=tenant.namespace,
                missing_ok=True,
            )
            print(f"[{self.log_prefix}] uninstalled release {tenant.name}")

        # Not part of the Helm release -- applied standalone in create(),
        # so it survives `helm uninstall` unless removed here too.
        self._kubectl(
            tenant, "delete", "networkpolicy", self._network_policy_name(tenant),
            "-n", tenant.namespace, "--ignore-not-found",
        )

        if delete_pvcs:
            removed = self.delete_pvcs(tenant)
            print(f"[{self.log_prefix}] deleted {len(removed)} PVC(s): {', '.join(removed)}")
        else:
            print(
                f"[{self.log_prefix}] PVCs left in place — pass delete_pvcs=True to "
                "remove the data too"
            )

    def endpoint(self, tenant: MinIOTenant) -> str:
        """In-cluster S3 URL. Hand this to `Inference.s3_endpoint_url`."""
        return tenant.endpoint()

    # -- storage ------------------------------------------------------------
    def list_pvcs(self, tenant: MinIOTenant) -> List[str]:
        """PVC names currently belonging to this tenant."""
        out = self._kubectl(
            tenant, "get", "pvc", "-n", tenant.namespace,
            "-l", tenant.label_selector,
            "-o", "jsonpath={range .items[*]}{.metadata.name}{'\\n'}{end}",
        )
        return [line for line in out.splitlines() if line.strip()]

    def pvc_sizes(self, tenant: MinIOTenant) -> Dict[str, str]:
        """`{pvc_name: requested_size}` for this tenant's PVCs."""
        out = self._kubectl(
            tenant, "get", "pvc", "-n", tenant.namespace,
            "-l", tenant.label_selector,
            "-o", "jsonpath={range .items[*]}{.metadata.name} "
                  "{.spec.resources.requests.storage}{'\\n'}{end}",
        )
        sizes: Dict[str, str] = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2:
                sizes[parts[0]] = parts[1]
        return sizes

    # MinIOStorageError from here is always pre-flight -- a shrink, or no
    # PVCs to resize. Both leave the tenant untouched.
    @track_update(name_of=lambda tenant: tenant.name,
                  refused=(MinIOStorageError,))
    def resize_storage(self, tenant: MinIOTenant, new_size: str) -> List[str]:
        """Grows this tenant's PVCs to `new_size`.

        Requires the StorageClass to have `allowVolumeExpansion: true`.
        Kubernetes cannot shrink a PVC, so a smaller size is rejected
        here before any call is made — the API server's own error for
        this is considerably less clear.
        """
        tenant.validate()
        sizes = self.pvc_sizes(tenant)
        if not sizes:
            raise MinIOStorageError(
                f"No PVCs found for tenant '{tenant.name}' in namespace "
                f"'{tenant.namespace}'."
            )

        target = parse_quantity(new_size)
        for pvc_name, current in sizes.items():
            if current and parse_quantity(current) > target:
                raise MinIOStorageError(
                    f"Cannot shrink PVC '{pvc_name}' from {current} to {new_size}; "
                    "Kubernetes does not support shrinking PVCs. To reduce size, "
                    "recreate the tenant with delete(delete_pvcs=True) then create()."
                )

        patch = json.dumps({"spec": {"resources": {"requests": {"storage": new_size}}}})
        for pvc_name in sizes:
            self._kubectl(
                tenant, "patch", "pvc", pvc_name, "-n", tenant.namespace,
                "--type", "merge", "-p", patch,
            )
        # Keep the spec consistent, so a later create() doesn't try to
        # set the pool back to the old size.
        tenant.volume_size = new_size
        return sorted(sizes)

    @track_update(name_of=lambda tenant: tenant.name)
    def delete_pvcs(self, tenant: MinIOTenant) -> List[str]:
        """Deletes this tenant's PVCs. Irreversible — the data goes with them."""
        tenant.validate()
        names = self.list_pvcs(tenant)
        for name in names:
            self._kubectl(
                tenant, "delete", "pvc", name, "-n", tenant.namespace,
                "--ignore-not-found",
            )
        return names

    # -- internals ----------------------------------------------------------
    def _install_operator(self, tenant: MinIOTenant) -> None:
        """Installs the cluster-scoped operator if it isn't already there."""
        if self._find_release(
            tenant, tenant.operator_release_name, tenant.operator_namespace
        ) is not None:
            return
        print(f"[{self.log_prefix}] installing operator {tenant.operator_release_name}")

        runner = self._helm_runner(tenant)

        runner.install_or_upgrade(
            tenant.operator_release_name,
            chart=tenant.operator_chart,
            namespace=tenant.operator_namespace,
        )


    def _deploy_tenant(self, tenant: MinIOTenant) -> None:
        """Installs or upgrades the tenant release."""
        runner = self._helm_runner(tenant)
        existing = self._find_release(tenant)
        if existing is not None and existing.status.value == "failed":
            # Helm refuses to upgrade over a failed release, so clear it
            # rather than leaving the caller with an unhelpful error.
            print(f"[{self.log_prefix}] clearing failed release {tenant.name}")

            runner.uninstall(
                tenant.name,
                namespace=tenant.namespace,
            )

        values_path = self._write_values(tenant)
        try:
            values = self._load_values(values_path)
            print(f"[{self.log_prefix}] deploying tenant {tenant.name} "
                  f"({tenant.servers}x{tenant.volumes_per_server} drives, "
                  f"{tenant.volume_size} each)")
            runner.install_or_upgrade(
                tenant.name,
                chart=tenant.tenant_chart,
                namespace=tenant.namespace,
                values=values,
            )

        finally:
            # The file holds the root credentials in cleartext.
            os.unlink(values_path)

    def _load_values(self, values_path: str) -> Dict[str, Any]:
        with open(values_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def _network_policy_name(self, tenant: MinIOTenant) -> str:
        return f"{tenant.name}-allow-clients"

    def _apply_network_policy(self, tenant: MinIOTenant) -> None:
        """Restricts the tenant's S3 port to `network_policy_allowed_ingress`.

        Applied standalone via `kubectl apply`, not through the tenant
        chart -- minio/tenant is a third-party chart and isn't ours to add
        templates to. `tenant.label_selector`'s key/value is what the
        operator itself puts on every pod it creates for this tenant, so
        this needs no cooperation from the chart at all.
        """
        # NetworkPolicy `ports` matches the destination port on the packet
        # arriving at the *pod* -- i.e. the Service's targetPort, after
        # Kubernetes DNATs it, not the Service's own advertised port. The
        # `minio` Service always targets container port 9000 regardless of
        # request_auto_cert (confirmed live: `kubectl get svc minio -o
        # yaml` shows targetPort: 9000 whether TLS is on or off -- MinIO
        # serves both http and https on the same container port). Using
        # 443/80 here (endpoint()'s *external* scheme) would leave 9000
        # unmatched by any `ports` entry, and because this policy's mere
        # existence puts the pod in default-deny-except-listed mode, that
        # blocks every client, allowed or not -- caught by an actual
        # positive-test failure against a live tenant, not a guess.
        port = 9000
        apply_network_policy(
            tenant.kubeconfig_path,
            self._network_policy_name(tenant),
            tenant.namespace,
            {"v1.min.io/tenant": tenant.name},
            allowed_ingress=tenant.network_policy_allowed_ingress,
            ports=[port],
            error_cls=MinIOError,
        )
        print(f"[{self.log_prefix}] restricted {tenant.name} ingress to "
              f"{len(tenant.network_policy_allowed_ingress)} selector(s) on "
              f"port {port}")

    def _write_values(self, tenant: MinIOTenant) -> str:
        """Writes the tenant values to a private temp file.

        JSON, because it is a subset of YAML that Helm parses happily and
        needs no third-party serializer. 0600 because the file contains
        the root credentials.
        """
        fd, path = tempfile.mkstemp(prefix=f"{tenant.name}-values-", suffix=".yaml")
        with os.fdopen(fd, "w") as handle:
            json.dump(tenant.helm_values(), handle, indent=2)
        os.chmod(path, 0o600)
        return path

    def _existing_credentials(self, tenant: MinIOTenant):
        """Read a live tenant's root credentials from its config secret.

        The operator stores them in a `config.env` blob of shell exports;
        older charts also wrote plain `accesskey`/`secretkey` entries, so
        both are handled. Returns None when the secret is absent or
        unreadable.
        """
        raw = self._kubectl(
            tenant, "get", "secret", tenant.config_secret_name,
            "-n", tenant.namespace, "-o", "json", check=False,
        )
        if not raw.strip():
            return None
        try:
            data = json.loads(raw).get("data", {})
        except json.JSONDecodeError:
            return None

        def decode(key):
            value = data.get(key)
            return base64.b64decode(value).decode("utf-8") if value else None

        env = decode("config.env")
        if env:
            found = dict(
                re.findall(r'^\s*export\s+(MINIO_ROOT_USER|MINIO_ROOT_PASSWORD)='
                           r'"?([^"\n]+)"?', env, re.MULTILINE)
            )
            user = found.get("MINIO_ROOT_USER")
            password = found.get("MINIO_ROOT_PASSWORD")
            if user and password:
                return user, password

        user, password = decode("accesskey"), decode("secretkey")
        return (user, password) if user and password else None

    def _wait_until_ready(self, tenant: MinIOTenant) -> List[str]:
        """Polls until every tenant pod is Running.

        Reports what is blocking rather than only timing out: an unbound
        PVC never resolves on its own, so waiting out the full timeout to
        say "not ready" wastes minutes and explains nothing.
        """
        deadline = time.time() + self.ready_timeout
        last = ""
        while True:
            pods = self._pod_phases(tenant)
            if pods and all(phase == "Running" for _, phase in pods):
                names = [name for name, _ in pods]
                print(f"[{self.log_prefix}] {len(names)} pod(s) running")
                return names

            summary = " ".join(f"{n}={p}" for n, p in pods) or "no pods yet"
            if summary != last:
                print(f"[{self.log_prefix}]   {summary}")
                last = summary

            if time.time() >= deadline:
                raise MinIOTimeoutError(
                    f"Tenant '{tenant.name}' was not ready within "
                    f"{self.ready_timeout}s.\n" + self._diagnose(tenant)
                )
            time.sleep(self.poll_interval)

    def _diagnose(self, tenant: MinIOTenant) -> str:
        """Explains why pods aren't Running — usually an unbound PVC."""
        lines: List[str] = []
        pending = self._kubectl(
            tenant, "get", "pvc", "-n", tenant.namespace,
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
            tenant, "get", "events", "-n", tenant.namespace,
            "--field-selector", "type=Warning",
            "-o", "jsonpath={range .items[*]}{.message}{'\\n'}{end}",
            check=False,
        ).strip()
        if events:
            lines.append("Recent warnings:")
            lines.extend(f"  {line}" for line in events.splitlines()[-5:])
        return "\n".join(lines) or "No PVC or event detail available."

    def _pod_phases(self, tenant: MinIOTenant) -> List[tuple]:
        out = self._kubectl(
            tenant, "get", "pods", "-n", tenant.namespace,
            "-l", tenant.label_selector,
            "-o", "jsonpath={range .items[*]}{.metadata.name} {.status.phase}{'\\n'}{end}",
            check=False,
        )
        phases = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2:
                phases.append((parts[0], parts[1]))
        return phases

    def _storage_classes(self, tenant: MinIOTenant) -> List[str]:
        out = self._kubectl(
            tenant, "get", "storageclass",
            "-o", "jsonpath={range .items[*]}{.metadata.name}{'\\n'}{end}",
            check=False,
        )
        return [line for line in out.splitlines() if line.strip()]

    def _default_storage_class(self, tenant: MinIOTenant) -> Optional[str]:
        out = self._kubectl(
            tenant, "get", "storageclass",
            "-o", "jsonpath={range .items[?(@.metadata.annotations."
                  "storageclass\\.kubernetes\\.io/is-default-class=='true')]}"
                  "{.metadata.name}{'\\n'}{end}",
            check=False,
        ).strip()
        return out.splitlines()[0] if out else None

    def _find_release(
        self,
        tenant: MinIOTenant,
        name: Optional[str] = None,
        namespace: Optional[str] = None,
    ) -> Optional[HelmRelease]:
        """The Helm release matching name+namespace, or None."""
        name = name or tenant.name
        namespace = namespace or tenant.namespace
        runner = self._helm_runner(tenant)

        return runner.get_release(
            name,
            namespace=namespace,
        )

    def _helm_runner(self, tenant: MinIOTenant) -> HelmRunner:
        return HelmRunner(
            tenant.kubeconfig_path,
        )

    def _kubectl(self, tenant: MinIOTenant, *args: str, check: bool = True) -> str:
        return self._local(
            ["kubectl", "--kubeconfig", tenant.kubeconfig_path, *args], check=check
        )

    def _local(self, argv: List[str], check: bool = True) -> str:
        """Runs a local CLI. Mechanics shared (see multistack/kube.py), so
        this inherits stdin being closed and a timeout raised as MinIOError
        rather than a bare subprocess.TimeoutExpired — neither of which
        this backend used to do."""
        return run_local(
            argv, check=check, timeout=self.command_timeout, error_cls=MinIOError
        )

    def _require_cli(self, name: str) -> None:
        # Nothing is executed: a presence check that ran `kubectl version`
        # could contact the API server and hang on an unreachable cluster.
        require_cli(
            name, error_cls=MinIOPrerequisiteError,
            purpose="this backend drives it directly",
        )
