"""One place to build a NetworkPolicy object.

Every capability that renders its own manifests (see
`multistack.inference.drivers.vllm.manifests`) builds them as plain dicts
shaped like the Kubernetes API and applies them with `multistack.kube`.
NetworkPolicy is no different -- this is that dict, not a new mechanism.

Chart-backed services (model-gateway, the rate limiters) do not use this:
their NetworkPolicy is a Helm template, and the Python side only fills in
the `.Values.networkPolicy.*` a driver's `_values()` already builds.

Ingress-only by design. Locking down egress needs an allowance for DNS
(cluster-specific: `kube-dns` vs `coredns` labels differ by install) and,
for vLLM, for whatever mirrors weights from S3 -- getting either wrong
silently breaks the pod instead of just under-restricting it. Restricting
who can call in is the safer default; egress lockdown is a deliberate
follow-up once those destinations are confirmed per-cluster.
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..kube import KubeCommandError
from ..kube import apply as kube_apply


def network_policy_manifest(
    name: str,
    namespace: str,
    pod_selector: Dict[str, str],
    *,
    allowed_ingress: List[Dict[str, str]],
    ports: List[int],
) -> Dict[str, Any]:
    """An ingress-only NetworkPolicy: only pods matching one of
    `allowed_ingress` (matchLabels dicts) may reach `pod_selector`'s pods,
    and only on `ports`. Egress is left unrestricted -- see module
    docstring.
    """
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "podSelector": {"matchLabels": pod_selector},
            "policyTypes": ["Ingress"],
            "ingress": [
                {
                    "from": [
                        {"podSelector": {"matchLabels": selector}}
                        for selector in allowed_ingress
                    ],
                    "ports": [
                        {"protocol": "TCP", "port": port} for port in ports
                    ],
                }
            ],
        },
    }


def apply_network_policy(
    kubeconfig_path: str,
    name: str,
    namespace: str,
    pod_selector: Dict[str, str],
    *,
    allowed_ingress: List[Dict[str, str]],
    ports: List[int],
    error_cls: type = KubeCommandError,
) -> None:
    """Builds and applies a NetworkPolicy in one call, for a workload that
    isn't rendered from one of our own charts -- so there's no `_values()`
    or template to attach this to, only the pod labels the workload
    already carries. `MinIOBackend` uses this for the (third-party)
    `minio/tenant` chart; any future chart-less driver (a tokenizer,
    admin-control-plane, ...) can call it the same way once it knows its
    own pods' labels.
    """
    manifest = network_policy_manifest(
        name, namespace, pod_selector,
        allowed_ingress=allowed_ingress, ports=ports,
    )
    kube_apply(kubeconfig_path, manifest, error_cls=error_cls)
