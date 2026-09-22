"""Billing, installed from its chart through the Helm layer.

Same shape as `FastAPIControlPlaneDriver`: a FastAPI service, a Postgres
schema, a migration Job that runs before the Deployment is ready, and a
Secret referenced by name rather than created here.
"""
import json
from typing import Any, Dict, List

from ...helm import HelmRunner
from ...kube import kubectl, require_cli, require_cluster
from ..base import BillingError, BillingPrerequisiteError
from ..spec import REQUIRED_SECRET_KEYS, Billing


class StripeBillingDriver:
    def __init__(self, ready_timeout: int = 300) -> None:
        self._ready_timeout = ready_timeout

    def _helm(self, billing: Billing) -> HelmRunner:
        return HelmRunner(billing.kubeconfig_path, error_cls=BillingError)

    def _values(self, billing: Billing) -> Dict[str, Any]:
        options = billing.options
        values: Dict[str, Any] = {
            "replicaCount": billing.replicas,
            "service": {"port": billing.service_port},
            "config": {
                "logLevel": billing.log_level,
                "events": {
                    "backboneUrl": billing.event_backbone_url,
                    "durableName": options.durable_name,
                },
                "allowNoBackbone": billing.allow_no_backbone,
                "stripe": {
                    "meterEventName": options.stripe_meter_event_name,
                    "meterCustomerField": options.stripe_meter_customer_field,
                },
                "reporter": {
                    "pollIntervalSeconds": options.reporter_poll_interval_seconds,
                    "batchSize": options.reporter_batch_size,
                    "maxAttempts": options.reporter_max_attempts,
                    "backoffBaseSeconds": options.reporter_backoff_base_seconds,
                    "backoffMaxSeconds": options.reporter_backoff_max_seconds,
                },
            },
            # By name only. create=false means the chart renders no Secret
            # of its own, so nothing here can put a credential in a
            # rendered manifest.
            "secret": {
                "existingSecret": billing.existing_secret,
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
        if billing.node_selector is not None:
            values["nodeSelector"] = billing.node_selector
        if billing.tolerations is not None:
            values["tolerations"] = billing.tolerations
        return values

    def check_prerequisites(self, billing: Billing) -> List[str]:
        warnings: List[str] = []
        require_cli("helm", error_cls=BillingPrerequisiteError)
        require_cluster(billing.kubeconfig_path, capability="billing")

        raw = kubectl(
            billing.kubeconfig_path, "get", "secret",
            billing.existing_secret,
            "-n", billing.resolved_namespace, "-o", "json",
            error_cls=BillingError, check=False,
        )
        if not (raw or "").strip():
            raise BillingPrerequisiteError(
                f"Secret '{billing.existing_secret}' does not exist in "
                f"namespace '{billing.resolved_namespace}'. It must hold "
                f"{', '.join(REQUIRED_SECRET_KEYS)}. Create it with "
                "multistack.kube.apply, which pipes the manifest to stdin "
                "— `kubectl create secret --from-literal` puts the values "
                "in the process arguments, where any local user can read "
                "them."
            )

        try:
            present = set(json.loads(raw).get("data", {}))
        except json.JSONDecodeError:
            return warnings

        missing = [k for k in REQUIRED_SECRET_KEYS if k not in present]
        if missing:
            raise BillingPrerequisiteError(
                f"Secret '{billing.existing_secret}' is missing "
                f"{', '.join(missing)}. The chart installs and the pod "
                "goes Ready regardless — SERVICE_API_KEY missing fails "
                "every /internal/v1/* call closed with a 500 on first "
                "use, never a silent bypass."
            )

        if "STRIPE_SECRET_KEY" not in present:
            warnings.append(
                "no STRIPE_SECRET_KEY in the Secret: the Stripe reporter "
                "is inert and outbox rows stay pending forever. Valid for "
                "usage-metering-only, but not a real Stripe integration."
            )
        if "STRIPE_WEBHOOK_SECRET" not in present:
            warnings.append(
                "no STRIPE_WEBHOOK_SECRET in the Secret: POST "
                "/webhooks/stripe returns 501 without reading the request "
                "body — subscription status will never sync from Stripe's "
                "own webhooks."
            )
        return warnings

    def create(self, billing: Billing) -> str:
        billing.validate()
        for warning in self.check_prerequisites(billing):
            print(f"[billing] warning: {warning}")

        self._helm(billing).install_or_upgrade(
            billing.options.release_name,
            chart=billing.options.chart,
            chart_version=billing.options.chart_version,
            namespace=billing.resolved_namespace,
            values=self._values(billing),
            strict_values=True,
        )
        return billing.endpoint

    def delete(self, billing: Billing) -> None:
        """Removes the release. The database is left alone -- it is a
        separate capability with its own lifecycle, and dropping a schema
        as a side effect of removing an API service is not something to
        do implicitly."""
        self._helm(billing).uninstall(
            billing.options.release_name,
            namespace=billing.resolved_namespace,
            missing_ok=True,
        )
