"""The enricher, installed from its chart through the Helm layer.

A JetStream consumer/republisher, not an HTTP service anything calls: it
reads the gateway's raw `response` events, guarantees a token count on
each (falling back to the tokenizer when `usage` is absent), and
republishes to the derived enriched stream that `rate-limiter-tpm` and
`billing` both consume from. `endpoint` exists for health checks and
metrics scraping only.
"""

from typing import Any

from ...helm import HelmRunner
from ...kube import require_cli, require_cluster
from ..base import EnricherError, EnricherPrerequisiteError
from ..spec import Enricher


class JetStreamEnricherDriver:
    def __init__(self, ready_timeout: int = 300) -> None:
        self._ready_timeout = ready_timeout

    def _helm(self, enricher: Enricher) -> HelmRunner:
        return HelmRunner(enricher.kubeconfig_path, error_cls=EnricherError)

    def _values(self, enricher: Enricher) -> dict[str, Any]:
        options = enricher.options
        values: dict[str, Any] = {
            "replicaCount": enricher.replicas,
            "service": {"port": enricher.service_port},
            "config": {
                "logLevel": "INFO",
                "events": {
                    "backboneUrl": enricher.event_backbone_url,
                    "streamName": options.event_stream_name,
                    "streamSubject": options.event_stream_subject,
                    "durableName": options.durable_name,
                    "enrichedStreamName": options.enriched_stream_name,
                    "enrichedSubject": options.enriched_subject,
                    "maxDeliver": options.max_deliver,
                    "ackWaitSeconds": options.ack_wait_seconds,
                },
                "tokenizer": {
                    "url": options.tokenizer_url,
                    "timeoutMs": options.tokenizer_timeout_ms,
                },
                "allowNoBackbone": enricher.allow_no_backbone,
            },
        }
        if options.image_tag or options.image_repository:
            image: dict[str, Any] = {}
            if options.image_repository:
                image["repository"] = options.image_repository
            if options.image_tag:
                image["tag"] = options.image_tag
            values["image"] = image
        # Absent leaves the chart's own default in place; an explicit {}
        # overrides it with "schedule anywhere", which is a different
        # statement and sometimes the intended one.
        if enricher.affinity is not None:
            # null, not {}, for the empty case: Helm merges user values
            # into the chart's recursively, so an empty map adds nothing and
            # the chart's own affinity survives -- the release installs and
            # the pod stays Pending with the default still in force. An
            # explicit null is what Helm treats as "delete this key".
            values["affinity"] = enricher.affinity or None
        if enricher.node_selector is not None:
            values["nodeSelector"] = enricher.node_selector
        if enricher.tolerations is not None:
            values["tolerations"] = enricher.tolerations
        return values

    def check_prerequisites(self, enricher: Enricher) -> list[str]:
        warnings: list[str] = []
        require_cli("helm", error_cls=EnricherPrerequisiteError)
        require_cluster(enricher.kubeconfig_path, capability="the enricher")

        if enricher.allow_no_backbone:
            warnings.append(
                "allow_no_backbone=True: this deploys consuming and "
                "republishing nothing. Deliberate here, but rate-limiter-tpm "
                "and billing will both see zero enrichment."
            )
        if not enricher.options.tokenizer_url:
            warnings.append(
                "tokenizer_url is empty, so the fallback is inert: "
                "response events with no `usage` block are republished "
                'with source="none" rather than estimated, and go '
                "uncounted downstream. Streaming responses are the usual "
                "case."
            )
        return warnings

    def create(self, enricher: Enricher) -> str:
        enricher.validate()
        for warning in self.check_prerequisites(enricher):
            print(f"[enricher] warning: {warning}")

        self._helm(enricher).install_or_upgrade(
            enricher.options.release_name,
            chart=enricher.options.chart,
            chart_version=enricher.options.chart_version,
            namespace=enricher.resolved_namespace,
            values=self._values(enricher),
            strict_values=True,
        )
        return enricher.endpoint

    def delete(self, enricher: Enricher) -> None:
        self._helm(enricher).uninstall(
            enricher.options.release_name,
            namespace=enricher.resolved_namespace,
            missing_ok=True,
        )
