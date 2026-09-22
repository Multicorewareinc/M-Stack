from .models import AcceleratorConfig, AcceleratorType


ACCELERATOR_CONFIGS: dict[
    AcceleratorType,
    AcceleratorConfig,
] = {
    AcceleratorType.TENSTORRENT: AcceleratorConfig(
        accelerator_type=AcceleratorType.TENSTORRENT,
        chart_reference=(
            "oci://ghcr.io/tenstorrent/helm/tt-operator"
        ),
    ),
    AcceleratorType.NVIDIA: AcceleratorConfig(
        accelerator_type=AcceleratorType.NVIDIA,
        chart_name="gpu-operator",
    ),
}