"""The first-party tokenizer, installed from its chart via the Helm layer."""

from typing import Any

from ...helm import HelmRunner
from ...kube import require_cli, require_cluster
from ..base import TokenizerError, TokenizerPrerequisiteError
from ..spec import Tokenizer


class TiktokenDriver:
    def __init__(self, ready_timeout: int = 300) -> None:
        self._ready_timeout = ready_timeout

    def _helm(self, tokenizer: Tokenizer) -> HelmRunner:
        return HelmRunner(tokenizer.kubeconfig_path, error_cls=TokenizerError)

    def _values(self, tokenizer: Tokenizer) -> dict[str, Any]:
        options = tokenizer.options
        values: dict[str, Any] = {
            "replicaCount": tokenizer.replicas,
            "service": {"port": tokenizer.service_port},
            "config": {"tokenizerDefault": options.default_encoding},
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
        if tokenizer.affinity is not None:
            # null, not {}, for the empty case: Helm merges user values
            # into the chart's recursively, so an empty map adds nothing and
            # the chart's own affinity survives -- the release installs and
            # the pod stays Pending with the default still in force. An
            # explicit null is what Helm treats as "delete this key".
            values["affinity"] = tokenizer.affinity or None
        if tokenizer.node_selector is not None:
            values["nodeSelector"] = tokenizer.node_selector
        if tokenizer.tolerations is not None:
            values["tolerations"] = tokenizer.tolerations
        return values

    def check_prerequisites(self, tokenizer: Tokenizer) -> list[str]:
        warnings: list[str] = []
        require_cli("helm", error_cls=TokenizerPrerequisiteError)
        require_cluster(tokenizer.kubeconfig_path, capability="a tokenizer")

        if tokenizer.replicas < 2:
            warnings.append(
                "replicas=1: the policy layer calls this on the request path "
                "for any response with no usage reported, so a single pod "
                "restarting takes token counting down with it."
            )
        return warnings

    def create(self, tokenizer: Tokenizer) -> str:
        tokenizer.validate()
        for warning in self.check_prerequisites(tokenizer):
            print(f"[tokenizer] warning: {warning}")

        self._helm(tokenizer).install_or_upgrade(
            tokenizer.options.release_name,
            chart=tokenizer.options.chart,
            chart_version=tokenizer.options.chart_version,
            namespace=tokenizer.resolved_namespace,
            values=self._values(tokenizer),
            strict_values=True,
        )
        return tokenizer.endpoint

    def delete(self, tokenizer: Tokenizer) -> None:
        self._helm(tokenizer).uninstall(
            tokenizer.options.release_name,
            namespace=tokenizer.resolved_namespace,
            missing_ok=True,
        )
