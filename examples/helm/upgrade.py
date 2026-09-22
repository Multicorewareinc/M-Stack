"""
Upgrade the release examples/helm/install.py created.

    python3 examples/helm/upgrade.py

install_or_upgrade() is the same call: it installs when the release is
absent and upgrades when it isn't, because a provisioning script that
fails on its second run is not much use.
"""
from multistack.helm import HelmRunner
import os

KUBECONFIG = os.path.expanduser(
    os.environ.get("MULTISTACK_KUBECONFIG", "~/.multistack/kubeconfig")
)
NAMESPACE = "helm-demo"

helm = HelmRunner(KUBECONFIG)

release = helm.install_or_upgrade(
    "demo-nginx",
    chart="nginx",
    namespace=NAMESPACE,
    values={
        "service": {"type": "ClusterIP"},
        "replicaCount": 2,
    },
)

print("Helm upgrade completed")
print(f"  release   : {release.name}")
print(f"  revision  : {release.revision}   (was 1 after install.py)")
print(f"  status    : {release.status.value}")
