"""
Uninstall an accelerator Helm release through the SDK's Helm layer.

    pip install -e ".[helm]"          # from the repo root
    python3 examples/helm/uninstall_accelerator.py

The accelerator type and chart are not required for uninstall. Helm removes
the existing release using its release name and namespace.
"""

import os

from multistack.helm import HelmRunner


KUBECONFIG = os.path.expanduser(
    os.environ.get("MULTISTACK_KUBECONFIG", "~/.multistack/kubeconfig")
)

# Supported values:
#   "TENSTORRENT"
#   "NVIDIA"
ACCELERATOR_TYPE = "TENSTORRENT"

NAMESPACE = (
    "tt-operator-system"
    if ACCELERATOR_TYPE == "TENSTORRENT"
    else "gpu-operator"
)

RELEASE_NAME = (
    "tt-operator"
    if ACCELERATOR_TYPE == "TENSTORRENT"
    else "gpu-operator"
)

helm = HelmRunner(KUBECONFIG)

helm.uninstall(
    RELEASE_NAME,
    namespace=NAMESPACE,
    wait=True,
    missing_ok=False,
)

print("Accelerator uninstall completed")
print(f"  accelerator : {ACCELERATOR_TYPE}")
print(f"  release     : {RELEASE_NAME}")
print(f"  namespace   : {NAMESPACE}")