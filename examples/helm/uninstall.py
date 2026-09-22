"""
Remove the release the other two examples here manage.

    python3 examples/helm/uninstall.py

Leaves the namespace behind — Helm removes releases, not namespaces.
"""
from multistack.helm import HelmRunner, HelmReleaseNotFoundError
import os

KUBECONFIG = os.path.expanduser(
    os.environ.get("MULTISTACK_KUBECONFIG", "~/.multistack/kubeconfig")
)
NAMESPACE = "helm-demo"

helm = HelmRunner(KUBECONFIG)

try:
    helm.uninstall("demo-nginx", namespace=NAMESPACE)
    print(f"Removed demo-nginx from {NAMESPACE}")
except HelmReleaseNotFoundError:
    # Reported rather than ignored. Pass missing_ok=True where a delete
    # genuinely should be idempotent — a teardown path, say — so that
    # "there was nothing there" is a decision instead of an accident.
    print(f"No demo-nginx release in {NAMESPACE} — nothing to remove")
