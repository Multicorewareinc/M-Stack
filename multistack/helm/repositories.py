"""
Default chart repositories, and the order they are searched in.

This is a *default*, not an allowlist. `ChartResolver` takes whatever
repositories it is given, because a repository URL is deployment
configuration — an internal mirror, a proxy, an air-gapped registry — not
a fact about the code. `HelmRunner` takes a `repositories=`
argument for exactly that reason, and a caller-supplied repository has to
be able to reach here.

Order matters more than it looks. The resolver walks the list and loads
each index until it finds the chart, so a repository placed after a large
one pays for that index on every miss. Measured on this lab: resolving
`longhorn` costs 0.2s with charts.longhorn.io first and 34.8s with
charts.bitnami.com first, because Bitnami's index.yaml is 27 MB against
Longhorn's 64 KB. Small and specific repositories go first.
"""
from .models import HelmRepository

LONGHORN_REPOSITORY = HelmRepository(
    name="longhorn",
    url="https://charts.longhorn.io",
)

NATS_REPOSITORY = HelmRepository(
    name="nats",
    url="https://nats-io.github.io/k8s/helm/charts/",
)

MINIO_OPERATOR_REPOSITORY = HelmRepository(
    name="minio-operator",
    url="https://operator.min.io",
)

CNPG_REPOSITORY = HelmRepository(
    name="cnpg",
    url="https://cloudnative-pg.github.io/charts",
)

PROMETHEUS_COMMUNITY_REPOSITORY = HelmRepository(
    name="prometheus-community",
    url="https://prometheus-community.github.io/helm-charts",
)

# Not the same place as NVIDIA_REPOSITORY below. That one is NGC, which
# carries the GPU Operator; the device plugin is published only here.
NVIDIA_DEVICE_PLUGIN_REPOSITORY = HelmRepository(
    name="nvdp",
    url="https://nvidia.github.io/k8s-device-plugin",
)

BITNAMI_REPOSITORY = HelmRepository(
    name="bitnami",
    url="https://charts.bitnami.com/bitnami",
)

NVIDIA_REPOSITORY = HelmRepository(
    name="nvidia",
    url="https://helm.ngc.nvidia.com/nvidia",
)

# Smallest and most specific first; see the note on ordering above.
DEFAULT_REPOSITORIES = (
    LONGHORN_REPOSITORY,
    NATS_REPOSITORY,
    MINIO_OPERATOR_REPOSITORY,
    CNPG_REPOSITORY,
    PROMETHEUS_COMMUNITY_REPOSITORY,
    # Ahead of Bitnami: its index is small, and a miss here would
    # otherwise pay for Bitnami's 27 MB one first.
    NVIDIA_DEVICE_PLUGIN_REPOSITORY,
    BITNAMI_REPOSITORY,
    NVIDIA_REPOSITORY,
)
