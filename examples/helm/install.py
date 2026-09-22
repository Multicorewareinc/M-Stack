"""
Install a Helm release through the SDK's Helm layer.

    pip install -e ".[helm]"          # from the repo root
    python3 examples/helm/install.py

Needs `helm` on PATH — pyhelm3 drives the binary rather than replacing it.
The chart is resolved by name against the repositories the runner is given;
pass your own to reach an internal mirror.

Nothing here is async, even though the layer underneath is. See
multistack/helm/__init__.py for why that boundary stops where it does.
"""
from multistack.helm import HelmRunner
import os

KUBECONFIG = os.path.expanduser(
    os.environ.get("MULTISTACK_KUBECONFIG", "~/.multistack/kubeconfig")
)
NAMESPACE = "helm-demo"

# Bound to one cluster at construction, so no call below can act on
# another. There is no ambient $KUBECONFIG fallback anywhere in this SDK.
helm = HelmRunner(KUBECONFIG)

release = helm.install_or_upgrade(
    "demo-nginx",
    chart="nginx",
    namespace=NAMESPACE,
    values={"service": {"type": "ClusterIP"}},
    # Default. A release that doesn't come up is rolled back rather than
    # left half-installed.
    atomic=True,
    create_namespace=True,
    # Strict value validation is enabled by default. Setting this to False
    # allows values that are not declared in the chart's values.yaml and
    # reports them as warnings instead of stopping the Helm operation. 
    strict_values=False,
)

print("Helm install completed")
print(f"  release   : {release.name}")
print(f"  namespace : {release.namespace}")
print(f"  revision  : {release.revision}")
print(f"  status    : {release.status.value}")
