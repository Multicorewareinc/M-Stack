"""Both control planes, installed from their charts via the Helm layer.

One driver serves both `type`s: the charts differ in which settings they
read, not in how they are installed, and the spec's options object
already carries the per-type chart path and release name.
"""
import base64
import json
from typing import Any, Dict, List

from ...helm import HelmRunner
from ...kube import kubectl, require_cli, require_cluster
from ..base import ControlPlaneError, ControlPlanePrerequisiteError
from ..spec import ControlPlane


class FastAPIControlPlaneDriver:
    def __init__(self, ready_timeout: int = 300) -> None:
        self._ready_timeout = ready_timeout

    def _helm(self, control_plane: ControlPlane) -> HelmRunner:
        return HelmRunner(
            control_plane.kubeconfig_path, error_cls=ControlPlaneError
        )

    def _values(self, control_plane: ControlPlane) -> Dict[str, Any]:
        options = control_plane.options
        config: Dict[str, Any] = {"logLevel": control_plane.log_level}

        # Each chart names the peer differently, because each is written
        # from its own side of the call.
        if control_plane.type == "admin":
            config["orgCpInternalUrl"] = control_plane.resolved_peer_url
            config["orgTimeoutMs"] = control_plane.peer_timeout_ms
            config["seedPlans"] = options.seed_plans
            config["seedPermissions"] = options.seed_permissions
        else:
            config["adminCpInternalUrl"] = control_plane.resolved_peer_url
            config["adminTimeoutMs"] = control_plane.peer_timeout_ms
        # Both charts have a billingInternalUrl hook now: admin-cp's best-effort
        # org/plan-change notify (ADR-032) and org-cp's usage read-through proxy
        # (add-org-cp-usage-proxy) each call billing independently.
        if control_plane.billing_internal_url:
            config["billingInternalUrl"] = control_plane.billing_internal_url

        values: Dict[str, Any] = {
            "replicaCount": control_plane.replicas,
            "service": {"port": control_plane.service_port},
            "config": config,
            # By name only. create=false means the chart renders no Secret
            # of its own, so nothing here can put a credential in a
            # rendered manifest.
            "secret": {
                "existingSecret": control_plane.existing_secret,
                "create": False,
            },
            "migration": {
                "activeDeadlineSeconds": options.migration_deadline_seconds,
                "backoffLimit": options.migration_backoff_limit,
            },
        }
        if options.image_tag:
            values["image"] = {"tag": options.image_tag}
        # Absent leaves the chart's own default in place; an explicit {}
        # overrides it with "schedule anywhere", which is a different
        # statement and sometimes the intended one.
        if control_plane.node_selector is not None:
            values["nodeSelector"] = control_plane.node_selector
        if control_plane.tolerations is not None:
            values["tolerations"] = control_plane.tolerations
        return values

    def check_prerequisites(self, control_plane: ControlPlane) -> List[str]:
        warnings: List[str] = []
        require_cli("helm", error_cls=ControlPlanePrerequisiteError)
        require_cluster(
            control_plane.kubeconfig_path, capability="a control plane"
        )

        raw = kubectl(
            control_plane.kubeconfig_path, "get", "secret",
            control_plane.existing_secret,
            "-n", control_plane.resolved_namespace, "-o", "json",
            error_cls=ControlPlaneError, check=False,
        )
        if not (raw or "").strip():
            raise ControlPlanePrerequisiteError(
                f"Secret '{control_plane.existing_secret}' does not exist in "
                f"namespace '{control_plane.resolved_namespace}'. It must "
                f"hold {', '.join(control_plane.required_secret_keys)}. "
                "Create it with multistack.kube.apply, which pipes the "
                "manifest to stdin — `kubectl create secret --from-literal` "
                "puts the values in the process arguments, where any local "
                "user can read them."
            )

        try:
            present = set(json.loads(raw).get("data", {}))
        except json.JSONDecodeError:
            return warnings

        missing = [k for k in control_plane.required_secret_keys
                   if k not in present]
        if missing:
            # Each of these fails a different way and none of them fail at
            # install: the pod starts, and the first request that needs the
            # value returns a 500.
            raise ControlPlanePrerequisiteError(
                f"Secret '{control_plane.existing_secret}' is missing "
                f"{', '.join(missing)}. The chart installs and the pod goes "
                "Ready regardless — VALKEY_URL missing makes every sign-in a "
                "500 because the login path checks lockout counters before "
                "verifying a password, and JWT_SECRET missing makes login "
                "fail closed rather than issue an unsigned token."
            )

        if (control_plane.type == "admin"
                and control_plane.replicas > 1
                and control_plane.options.seed_plans):
            warnings.append(
                "seed_plans=True with replicas>1: seeding is idempotent per "
                "row but not concurrency-safe, so two pods racing on a first "
                "install both insert the same plan and one dies on the "
                "unique index. Install with replicas=1, or seed first."
            )
        return warnings

    def create(self, control_plane: ControlPlane) -> str:
        control_plane.validate()
        for warning in self.check_prerequisites(control_plane):
            print(f"[controlplane] warning: {warning}")

        self._helm(control_plane).install_or_upgrade(
            control_plane.options.release_name,
            chart=control_plane.options.chart,
            chart_version=control_plane.options.chart_version,
            namespace=control_plane.resolved_namespace,
            values=self._values(control_plane),
            strict_values=True,
        )
        return control_plane.endpoint

    def delete(self, control_plane: ControlPlane) -> None:
        """Removes the release. The database is left alone -- it is a
        separate capability with its own lifecycle, and dropping a schema
        as a side effect of removing an API service is not something to do
        implicitly."""
        self._helm(control_plane).uninstall(
            control_plane.options.release_name,
            namespace=control_plane.resolved_namespace,
            missing_ok=True,
        )
