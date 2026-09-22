"""CloudNativePG, installed from its own chart via the Helm layer.

Manages:
- The CloudNativePG operator's lifecycle through the common Helm SDK.
- PostgreSQL Cluster custom resources through kubectl.

The two lifecycles are intentionally kept separate:

    operator   create() / update() / delete()
    cluster    create_cluster() / update_cluster() / delete_cluster()

CloudNativePG CRDs are intentionally retained when the operator is
uninstalled -- they are cluster-scoped and shared, so dropping them
would take every PostgreSQL Cluster on the cluster with them.

Ported from `backends/cnpg_client.py` unchanged apart from its shape:
the state-tracking decorators moved up to `DatabaseBackend` in
`registry.py`, where every migrated capability keeps them; the operator
settings now read off `spec.options` through the spec's own properties;
and the Cluster's namespace reads `resolved_namespace` rather than a
bare `namespace` field.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ...helm import HelmRelease, HelmRunner
from ...kube import (
    apply,
    require_cli,
    require_cluster,
    run_local,
    wait_for,
    wait_for_absent,
)
from ..base import (
    DatabaseClusterNotFoundError,
    DatabaseError,
    DatabasePrerequisiteError,
    DatabaseTimeoutError,
)
from ..spec import Database


CLUSTER_GROUP = "postgresql.cnpg.io"
CLUSTER_VERSION = "v1"
CLUSTER_PLURAL = "clusters"
CLUSTER_RESOURCE = f"{CLUSTER_PLURAL}.{CLUSTER_GROUP}"

HEALTHY_PHASE = "Cluster in healthy state"


class CNPGDriver:
    """Creates and manages CloudNativePG operators and clusters."""

    # Named for the implementation, not the capability, following
    # storage's `[longhorn]` and the cache's `[valkey]` -- it tells a
    # reader which database is talking, and means migrating changed no
    # visible output.
    log_prefix = "cnpg"

    def __init__(self, command_timeout: int = 600):
        self.command_timeout = command_timeout

    # ------------------------------------------------------------------
    # Operator lifecycle
    # ------------------------------------------------------------------

    def create(self, cnpg: Database) -> HelmRelease:
        """
        Install the CloudNativePG operator.

        If the Helm release already exists, the existing release is
        returned without performing another Helm operation.

        This method manages only the operator. It does not create a
        PostgreSQL Cluster.
        """
        self.check_prerequisites(cnpg)

        existing = self._find_operator_release(cnpg)

        if existing is not None:
            print(
                f"[{self.log_prefix}] operator {cnpg.operator_release_name} "
                f"already installed in {cnpg.operator_namespace}"
            )

            self._verify_crd(cnpg)
            return existing

        runner = self._helm_runner(cnpg)

        release = runner.install_or_upgrade(
            cnpg.operator_release_name,
            chart=cnpg.operator_chart,
            namespace=cnpg.operator_namespace,
            chart_version=cnpg.operator_chart_version,
            values=cnpg.values,
            atomic=True,
            wait=True,
            create_namespace=True,
        )

        self._verify_crd(cnpg)

        print(
            f"[{self.log_prefix}] installed operator "
            f"{cnpg.operator_release_name} in {cnpg.operator_namespace}"
        )

        return release

    def update(self, cnpg: Database) -> HelmRelease:
        """
        Install or upgrade the CloudNativePG operator.

        This method manages only the operator. It does not update any
        PostgreSQL Cluster resources.
        """
        self.check_prerequisites(cnpg)

        runner = self._helm_runner(cnpg)

        release = runner.install_or_upgrade(
            cnpg.operator_release_name,
            chart=cnpg.operator_chart,
            namespace=cnpg.operator_namespace,
            chart_version=cnpg.operator_chart_version,
            values=cnpg.values,
            atomic=True,
            wait=True,
            create_namespace=True,
        )

        self._verify_crd(cnpg)

        print(
            f"[{self.log_prefix}] updated operator "
            f"{cnpg.operator_release_name} in {cnpg.operator_namespace}"
        )

        return release

    def delete(
        self,
        cnpg: Database,
        *,
        wait: bool = True,
        remove_namespace: bool = True,
    ) -> None:
        """
        Uninstall the CloudNativePG operator.

        The operator namespace is removed by default after Helm
        uninstall.

        CloudNativePG CRDs are intentionally retained.

        PostgreSQL Cluster resources must be managed separately through
        delete_cluster().
        """
        self.check_prerequisites(cnpg)

        runner = self._helm_runner(cnpg)

        runner.uninstall(
            cnpg.operator_release_name,
            namespace=cnpg.operator_namespace,
            wait=wait,
            missing_ok=True,
        )

        print(
            f"[{self.log_prefix}] uninstalled operator "
            f"{cnpg.operator_release_name} from {cnpg.operator_namespace}"
        )

        if remove_namespace:
            self._delete_operator_namespace(cnpg)

    # ------------------------------------------------------------------
    # PostgreSQL Cluster lifecycle
    # ------------------------------------------------------------------

    def create_cluster(
        self,
        cnpg: Database,
        *,
        wait_for_ready: bool = True,
    ) -> Database:
        """
        Create a PostgreSQL Cluster managed by CloudNativePG.

        The CloudNativePG operator must already be installed.
        """
        self.check_cluster_prerequisites(cnpg)
        self._verify_crd(cnpg)

        if self.cluster_exists(cnpg):
            print(
                f"[{self.log_prefix}] cluster {cnpg.name} already exists "
                f"in {cnpg.resolved_namespace}"
            )
            return cnpg

        self._ensure_namespace(cnpg)
        self._apply_secret(cnpg)
        self._apply_cluster(cnpg)

        if wait_for_ready:
            self.wait_for_cluster_ready(cnpg)

        print(
            f"[{self.log_prefix}] cluster {cnpg.name} ready in "
            f"{cnpg.resolved_namespace} at {cnpg.endpoint}"
        )

        return cnpg

    def update_cluster(
        self,
        cnpg: Database,
        *,
        wait_for_ready: bool = True,
    ) -> Database:
        """
        Update an existing PostgreSQL Cluster.

        The desired Cluster state is taken from the spec and
        applied declaratively.
        """
        self.check_cluster_prerequisites(cnpg)
        self._verify_crd(cnpg)

        if not self.cluster_exists(cnpg):
            raise DatabaseClusterNotFoundError(
                f"CloudNativePG Cluster '{cnpg.name}' was not found "
                f"in namespace '{cnpg.resolved_namespace}'."
            )

        self._apply_cluster(cnpg)

        if wait_for_ready:
            self.wait_for_cluster_ready(cnpg)

        print(f"[{self.log_prefix}] cluster {cnpg.name} updated")

        return cnpg

    def delete_cluster(
        self,
        cnpg: Database,
        *,
        remove_secret: bool = True,
        wait: bool = True,
    ) -> None:
        """
        Delete a PostgreSQL Cluster custom resource.

        This method does not uninstall the CloudNativePG operator and
        does not delete CloudNativePG CRDs.
        """
        self.check_cluster_prerequisites(cnpg, bootstrap=False)
        self._verify_crd(cnpg)

        if not self.cluster_exists(cnpg):
            print(
                f"[{self.log_prefix}] cluster {cnpg.name} is already absent "
                f"from {cnpg.resolved_namespace}"
            )
        else:
            self._kubectl(
                cnpg,
                "delete",
                CLUSTER_RESOURCE,
                cnpg.name,
                "-n",
                cnpg.resolved_namespace,
                "--ignore-not-found=true",
            )

            print(
                f"[{self.log_prefix}] deleting cluster {cnpg.name} from "
                f"{cnpg.resolved_namespace}"
            )

            if wait:
                self.wait_for_cluster_deleted(cnpg)

        if remove_secret:
            self._delete_secret(cnpg)

    # ------------------------------------------------------------------
    # Cluster status
    # ------------------------------------------------------------------

    def cluster_exists(self, cnpg: Database) -> bool:
        """Return True when the PostgreSQL Cluster exists."""
        cnpg.validate_cluster_identity()

        output = self._kubectl(
            cnpg,
            "get",
            CLUSTER_RESOURCE,
            cnpg.name,
            "-n",
            cnpg.resolved_namespace,
            "-o",
            "name",
            check=False,
        )

        return bool(output.strip())

    def cluster_status(self, cnpg: Database) -> Dict[str, Any]:
        """Return the PostgreSQL Cluster resource as a dictionary."""
        cnpg.validate_cluster_identity()

        output = self._kubectl(
            cnpg,
            "get",
            CLUSTER_RESOURCE,
            cnpg.name,
            "-n",
            cnpg.resolved_namespace,
            "-o",
            "json",
            check=False,
        )

        if not output.strip():
            raise DatabaseClusterNotFoundError(
                f"CloudNativePG Cluster '{cnpg.name}' was not found "
                f"in namespace '{cnpg.resolved_namespace}'."
            )

        try:
            return json.loads(output)
        except json.JSONDecodeError as exc:
            raise DatabaseError(
                f"Failed to parse status for CloudNativePG "
                f"Cluster '{cnpg.name}'."
            ) from exc

    # ------------------------------------------------------------------
    # Cluster readiness
    # ------------------------------------------------------------------

    def wait_for_cluster_ready(self, cnpg: Database) -> None:
        """Wait until the PostgreSQL Cluster reaches the healthy phase."""
        cnpg.validate_cluster_identity()

        def _is_ready() -> bool:
            try:
                cluster = self.cluster_status(cnpg)
            except DatabaseClusterNotFoundError:
                return False

            phase = cluster.get("status", {}).get("phase")
            return phase == HEALTHY_PHASE

        try:
            wait_for(
                _is_ready,
                timeout=cnpg.ready_timeout,
                interval=cnpg.ready_poll_interval,
                description=(
                    f"CNPG Cluster '{cnpg.name}' to become ready"
                ),
            )
        except TimeoutError as exc:
            raise DatabaseTimeoutError(
                f"Timed out waiting for CloudNativePG Cluster "
                f"'{cnpg.name}' in namespace '{cnpg.resolved_namespace}' "
                f"to become ready."
            ) from exc

    def wait_for_cluster_deleted(self, cnpg: Database) -> None:
        """Wait until the PostgreSQL Cluster resource is absent.

        `wait_for_absent` takes the object to watch, not a predicate:
        (kubeconfig_path, kind, name, namespace). Called with a lambda
        it raised TypeError for three missing positional arguments --
        so delete_cluster(), whose `wait` defaults to True, could never
        complete.
        """
        cnpg.validate_cluster_identity()

        try:
            wait_for_absent(
                cnpg.kubeconfig_path,
                CLUSTER_RESOURCE,
                cnpg.name,
                cnpg.resolved_namespace,
                timeout=cnpg.ready_timeout,
                interval=cnpg.ready_poll_interval,
                error_cls=DatabaseError,
            )
        except TimeoutError as exc:
            raise DatabaseTimeoutError(
                f"Timed out waiting for CloudNativePG Cluster "
                f"'{cnpg.name}' in namespace '{cnpg.resolved_namespace}' "
                f"to be deleted."
            ) from exc

    # ------------------------------------------------------------------
    # Prerequisites
    # ------------------------------------------------------------------

    def check_prerequisites(self, cnpg: Database) -> None:
        """Validate prerequisites required for operator operations."""
        cnpg.validate()

        self._require_cli("helm")
        self._require_cli("kubectl")

        require_cluster(
            cnpg.kubeconfig_path,
            capability="the CloudNativePG operator",
            timeout=self.command_timeout,
        )

    def check_cluster_prerequisites(
        self,
        cnpg: Database,
        *,
        bootstrap: bool = True,
    ) -> None:
        """Validate prerequisites required for Cluster operations.

        `bootstrap=False` for the paths that only name a Cluster --
        deleting one should not require its password.
        """
        if bootstrap:
            cnpg.validate_cluster()
        else:
            cnpg.validate_cluster_identity()

        self._require_cli("kubectl")

        require_cluster(
            cnpg.kubeconfig_path,
            capability="a PostgreSQL cluster",
            timeout=self.command_timeout,
        )

    # ------------------------------------------------------------------
    # Kubernetes helpers
    # ------------------------------------------------------------------

    def _verify_crd(self, cnpg: Database) -> None:
        """Verify that the CloudNativePG Cluster CRD exists."""
        output = self._kubectl(
            cnpg,
            "get",
            "crd",
            cnpg.crd_name,
            "-o",
            "name",
            check=False,
        )

        if not output.strip():
            raise DatabasePrerequisiteError(
                f"CloudNativePG CRD '{cnpg.crd_name}' was not found."
            )

    def _ensure_namespace(self, cnpg: Database) -> None:
        """Create the PostgreSQL Cluster namespace if necessary."""
        output = self._kubectl(
            cnpg,
            "get",
            "namespace",
            cnpg.resolved_namespace,
            "-o",
            "name",
            check=False,
        )

        if output.strip():
            return

        self._kubectl(
            cnpg,
            "create",
            "namespace",
            cnpg.resolved_namespace,
        )

    def _delete_operator_namespace(self, cnpg: Database) -> None:
        """
        Delete the CloudNativePG operator namespace.

        CloudNativePG CRDs are cluster-scoped resources and are
        intentionally not deleted by this operation.
        """
        output = self._kubectl(
            cnpg,
            "get",
            "namespace",
            cnpg.operator_namespace,
            "-o",
            "name",
            check=False,
        )

        if not output.strip():
            print(
                f"[{self.log_prefix}] namespace {cnpg.operator_namespace} "
                f"is already absent"
            )
            return

        self._kubectl(
            cnpg,
            "delete",
            "namespace",
            cnpg.operator_namespace,
            "--ignore-not-found=true",
        )

        print(
            f"[{self.log_prefix}] deleted namespace {cnpg.operator_namespace}"
        )

    def _apply_secret(self, cnpg: Database) -> None:
        """Create or update the PostgreSQL bootstrap Secret."""
        if cnpg.database is None:
            raise DatabaseError(
                "Database configuration is required to create "
                "the PostgreSQL bootstrap Secret."
            )

        manifest = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": cnpg.secret_name,
                "namespace": cnpg.resolved_namespace,
            },
            "type": "kubernetes.io/basic-auth",
            "stringData": {
                "username": cnpg.database.owner,
                # The one place the real value is read. It goes
                # straight into the Secret and nowhere else.
                "password": cnpg.database.password.get_secret_value(),
            },
        }

        apply(
            cnpg.kubeconfig_path,
            manifest,
            timeout=self.command_timeout,
            error_cls=DatabaseError,
        )

    def _delete_secret(self, cnpg: Database) -> None:
        """Delete the PostgreSQL bootstrap Secret."""
        self._kubectl(
            cnpg,
            "delete",
            "secret",
            cnpg.secret_name,
            "-n",
            cnpg.resolved_namespace,
            "--ignore-not-found=true",
        )

    def _apply_cluster(self, cnpg: Database) -> None:
        """Create or update the PostgreSQL Cluster resource."""
        manifest = self._build_cluster_manifest(cnpg)

        apply(
            cnpg.kubeconfig_path,
            manifest,
            timeout=self.command_timeout,
            error_cls=DatabaseError,
        )

    def _build_cluster_manifest(
        self,
        cnpg: Database,
    ) -> Dict[str, Any]:
        """Build the CloudNativePG Cluster manifest."""
        cnpg.validate_cluster()

        if cnpg.database is None:
            raise DatabaseError(
                "Database configuration is required to build "
                "the PostgreSQL Cluster manifest."
            )

        storage: Dict[str, Any] = {
            "size": cnpg.storage_size,
        }

        if cnpg.storage_class:
            storage["storageClass"] = cnpg.storage_class

        # CloudNativePG emits no scheduling constraints of its own, so
        # whatever the spec carries is the only thing keeping Postgres
        # off the control plane.
        scheduling: Dict[str, Any] = {}
        if cnpg.affinity:
            scheduling["affinity"] = cnpg.affinity
        if cnpg.node_selector:
            scheduling.setdefault("affinity", {})
            scheduling["affinity"]["nodeSelector"] = dict(cnpg.node_selector)
        if cnpg.tolerations:
            scheduling.setdefault("affinity", {})
            scheduling["affinity"]["tolerations"] = list(cnpg.tolerations)

        return {
            "apiVersion": f"{CLUSTER_GROUP}/{CLUSTER_VERSION}",
            "kind": "Cluster",
            "metadata": {
                "name": cnpg.name,
                "namespace": cnpg.resolved_namespace,
            },
            "spec": {
                "instances": cnpg.instances,
                "imageName": cnpg.image,
                "storage": storage,
                **scheduling,
                "bootstrap": {
                    "initdb": {
                        "database": cnpg.database.name,
                        "owner": cnpg.database.owner,
                        "secret": {
                            "name": cnpg.secret_name,
                        },
                    },
                },
            },
        }

    # ------------------------------------------------------------------
    # Helm helpers
    # ------------------------------------------------------------------

    def _find_operator_release(
        self,
        cnpg: Database,
    ) -> Optional[HelmRelease]:
        """Return the existing operator Helm release, if present."""
        runner = self._helm_runner(cnpg)

        return runner.get_release(
            cnpg.operator_release_name,
            namespace=cnpg.operator_namespace,
        )

    def _helm_runner(self, cnpg: Database) -> HelmRunner:
        """Create the common Helm runner for the target cluster."""
        return HelmRunner(
            cnpg.kubeconfig_path,
            timeout=cnpg.install_timeout,
        )

    # ------------------------------------------------------------------
    # Command helpers
    # ------------------------------------------------------------------

    def _kubectl(
        self,
        cnpg: Database,
        *args: str,
        check: bool = True,
    ) -> str:
        """Run kubectl against the configured Kubernetes cluster."""
        return self._local(
            [
                "kubectl",
                "--kubeconfig",
                cnpg.kubeconfig_path,
                *args,
            ],
            check=check,
        )

    def _local(
        self,
        argv: List[str],
        check: bool = True,
    ) -> str:
        """Execute a local command."""
        return run_local(
            argv,
            check=check,
            timeout=self.command_timeout,
            error_cls=DatabaseError,
        )

    def _require_cli(self, name: str) -> None:
        """Verify that a required CLI is available."""
        require_cli(
            name,
            error_cls=DatabasePrerequisiteError,
            purpose="this driver drives it directly",
        )