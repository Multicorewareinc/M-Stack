"""
kube-prometheus-stack: Prometheus, Alertmanager and Grafana from one Helm
chart, plus node-exporter and kube-state-metrics for cluster-wide
scraping out of the box.

Driven entirely through the Helm layer in `multistack.helm` — no raw
`helm`/`kubectl` subprocess calls of its own for the install itself,
following the same shape as the ingress_gateway driver.
`PROMETHEUS_COMMUNITY_REPOSITORY` (defined once in
`multistack/helm/repositories.py`, and already part of `HelmRunner`'s
default repository list) is the chart source; nothing here adds a second
one.

Requirements on the machine running this: `helm` on PATH, and network
access to the chart source above — plus everything `HelmRunner` itself
needs (see `multistack/helm/__init__.py`).
"""
from __future__ import annotations

import base64
import json
import secrets
import string
from typing import Any, Dict, List, Optional, Tuple

from ...helm import HelmRunner, PROMETHEUS_COMMUNITY_REPOSITORY
from ...kube import kubectl, require_cli, require_cluster
from ..base import ObservabilityError, ObservabilityPrerequisiteError
from ..spec import Observability


def _generate_password(length: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


class KubePrometheusStackDriver:
    """Installs and removes the kube-prometheus-stack release."""

    def __init__(self, install_timeout: int = 600) -> None:
        # How long `helm --wait` blocks for every pod (Prometheus,
        # Alertmanager, Grafana, node-exporter, kube-state-metrics) to come
        # up. Generous because a cold cluster pulls several images at once.
        self._install_timeout = install_timeout

    def _helm(self, observability: Observability) -> HelmRunner:
        return HelmRunner(
            observability.kubeconfig_path,
            repositories=(PROMETHEUS_COMMUNITY_REPOSITORY,),
            timeout=f"{self._install_timeout}s",
            error_cls=ObservabilityError,
        )

    # -- prerequisites ------------------------------------------------------
    def check_prerequisites(self, observability: Observability) -> List[str]:
        """Verifies the cluster can host this release.

        Returns warnings; raises `ObservabilityPrerequisiteError` on
        anything that would leave observability unusable.
        """
        require_cli(
            "helm", error_cls=ObservabilityPrerequisiteError,
            purpose="the observability driver drives it directly",
        )
        require_cluster(
            observability.kubeconfig_path, capability="observability",
        )
        return []

    # -- lifecycle ------------------------------------------------------
    def create(self, observability: Observability) -> str:
        """
        Installs (or upgrades) kube-prometheus-stack in the cluster
        `observability` names and returns the Grafana endpoint.

        Idempotent: uses `helm upgrade --install`, so re-running a
        provisioning script reconciles the release to the spec rather than
        failing. An existing release's Grafana admin credentials are read
        back rather than regenerated — see `_resolve_grafana_credentials`.
        """
        observability.validate()
        for warning in self.check_prerequisites(observability):
            print(f"[observability] warning: {warning}")

        self._resolve_grafana_credentials(observability)

        helm = self._helm(observability)
        release = observability.options.release_name
        namespace = observability.resolved_namespace

        print(
            f"[observability] installing {observability.options.chart} into "
            f"{namespace} (retention={observability.metrics_retention}, "
            "may take several minutes)"
        )
        helm.install_or_upgrade(
            release,
            chart=observability.options.chart,
            chart_version=observability.chart_version,
            namespace=namespace,
            values=self._helm_values(observability),
            # The Grafana subchart's shipped values.yaml documents
            # `adminPassword` as a comment (`# adminPassword:
            # strongpassword`) rather than a real default, specifically so
            # a fresh install never ships a guessable one -- so it is
            # absent from the parsed defaults HelmValuesValidator checks
            # against even though the chart genuinely reads it. Strict mode
            # would reject it as unsupported on every install; see the
            # `HelmValuesValidator` docstring in `multistack/helm/values.py`
            # for the general case this is an instance of.
            strict_values=False,
        )

        endpoint = observability.grafana_endpoint()
        print(
            f"[observability] ready — Grafana at {endpoint} "
            f"(user={observability.grafana_admin_user})"
        )
        return endpoint

    def delete(self, observability: Observability) -> None:
        """
        Uninstalls the kube-prometheus-stack release.

        Deliberately does not delete the PVCs Prometheus/Alertmanager/
        Grafana bound: `helm uninstall` never removes them, so metrics and
        dashboards survive a teardown unless removed by hand — the same
        contract `StorageBackend.delete()` gives volumes.
        """
        observability.validate()
        require_cli(
            "helm", error_cls=ObservabilityPrerequisiteError,
            purpose="the observability driver drives it directly",
        )

        helm = self._helm(observability)
        print(
            f"[observability] uninstalling release "
            f"'{observability.options.release_name}'"
        )
        helm.uninstall(
            observability.options.release_name,
            namespace=observability.resolved_namespace,
            missing_ok=True,
        )
        print(
            "[observability] release removed. Persisted metrics/dashboards "
            "are left in place on their PVCs — delete those manually if you "
            "mean to discard the data too."
        )

    # -- values ---------------------------------------------------------
    def _helm_values(self, observability: Observability) -> Dict[str, Any]:
        """Maps the generic Observability spec onto the chart's values.

        This is the translation layer that keeps `Observability`
        implementation-neutral: `metrics_retention` means the same thing
        to every driver, but only this one knows it is spelled
        `prometheus.prometheusSpec.retention`. `extra_values` is merged
        last (shallow, like `LonghornOptions`/`MinIOTenant`) so it can
        override anything modelled here.
        """
        storage_class = observability.storage_class

        def volume_claim(size: str) -> Dict[str, Any]:
            spec: Dict[str, Any] = {
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": size}},
            }
            if storage_class:
                spec["storageClassName"] = storage_class
            return {"spec": spec}

        values: Dict[str, Any] = {
            "grafana": {
                "adminUser": observability.grafana_admin_user,
                "adminPassword": observability.grafana_admin_password,
                "service": {"type": observability.options.grafana_service_type},
                "persistence": {
                    "enabled": True,
                    "size": observability.grafana_volume_size,
                    **({"storageClassName": storage_class} if storage_class else {}),
                },
            },
            "prometheus": {
                "prometheusSpec": {
                    "retention": observability.metrics_retention,
                    "storageSpec": {
                        "volumeClaimTemplate": volume_claim(
                            observability.prometheus_volume_size
                        ),
                    },
                },
            },
            "alertmanager": {
                "alertmanagerSpec": {
                    "storage": {
                        "volumeClaimTemplate": volume_claim(
                            observability.alertmanager_volume_size
                        ),
                    },
                },
            },
        }
        values.update(observability.extra_values)
        return values

    # -- Grafana credentials ---------------------------------------------
    def _resolve_grafana_credentials(self, observability: Observability) -> None:
        """Fills in a Grafana admin password, generating one only when
        neither the caller nor a live release already has one.

        An existing release's credentials must be read back, never
        regenerated: the chart writes the password into a Secret at
        install time, and re-running `helm upgrade` with a freshly
        generated one would rotate a live Grafana's admin password out
        from under anyone already using it — the same hazard
        `MinIOBackend.create()` guards against for tenant root
        credentials.
        """
        if observability.grafana_admin_password:
            return

        existing = self._existing_credentials(observability)
        if existing:
            user, password = existing
            observability.set_grafana_credentials(user, password)
            print(
                f"[observability] reusing existing Grafana credentials for "
                f"{observability.options.release_name}"
            )
            return

        observability.set_grafana_credentials(
            observability.grafana_admin_user, _generate_password()
        )

    def _existing_credentials(
        self, observability: Observability
    ) -> Optional[Tuple[str, str]]:
        """Reads a live release's Grafana admin credentials from its
        Secret. Returns None when the secret is absent or unreadable —
        including "no such release yet", which is the common case."""
        secret_name = f"{observability.options.release_name}-grafana"
        raw = kubectl(
            observability.kubeconfig_path, "get", "secret", secret_name,
            "-n", observability.resolved_namespace, "-o", "json",
            check=False, error_cls=ObservabilityError,
        )
        if not raw.strip():
            return None
        try:
            data = json.loads(raw).get("data", {})
        except json.JSONDecodeError:
            return None

        def decode(key: str) -> Optional[str]:
            value = data.get(key)
            return base64.b64decode(value).decode("utf-8") if value else None

        user, password = decode("admin-user"), decode("admin-password")
        return (user, password) if user and password else None
