from enum import Enum

from pydantic import BaseModel, ConfigDict


class AcceleratorType(str, Enum):
    TENSTORRENT = "TENSTORRENT"
    NVIDIA = "NVIDIA"


class AcceleratorConfig(BaseModel):
    """
    Static configuration for a supported accelerator.

    The SDK owns the Helm chart reference.
    """

    model_config = ConfigDict(frozen=True)

    accelerator_type: AcceleratorType
    chart_reference: str | None = None
    chart_name: str | None = None