"""
Install an accelerator Helm release through the SDK's Helm layer.

    pip install -e ".[helm]"          # from the repo root
    python3 examples/helm/install_accelerator.py

Needs `helm` on PATH — pyhelm3 drives the binary rather than replacing it.

The accelerator type determines which Helm chart is installed:
  - tenstorrent -> Tenstorrent operator OCI chart
  - nvidia      -> NVIDIA GPU Operator Helm chart

The SDK handles accelerator prerequisites, chart resolution, default values,
validation, and Helm deployment.
"""

import os

from multistack.helm import HelmRunner


# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------

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

# ------------------------------------------------------------------
# Helm deployment
# ------------------------------------------------------------------

helm = HelmRunner(KUBECONFIG)

release = helm.install_or_upgrade(
    RELEASE_NAME,
    accelerator_type=ACCELERATOR_TYPE,
    namespace=NAMESPACE,
    values={},
    atomic=True,
    create_namespace=True,
)


# ------------------------------------------------------------------
# Result
# ------------------------------------------------------------------

print("Accelerator installation completed")
print(f"  accelerator : {ACCELERATOR_TYPE}")
print(f"  release     : {release.name}")
print(f"  namespace   : {release.namespace}")
print(f"  revision    : {release.revision}")
print(f"  status      : {release.status.value}")
