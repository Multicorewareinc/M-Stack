"""What exists, and the backend that dispatches to it."""
from typing import Any, Dict, List, Optional

from ..capability import CapabilityBackend
from ..state.tracking import track_create, track_delete
from .base import InferenceError
from .spec import Inference

# Named, not imported: a driver loads only when its type is selected.
DRIVERS: Dict[str, Any] = {
    "vllm": ("multistack.inference.drivers.vllm", "VLLMDriver"),
    "llamacpp": ("multistack.inference.drivers.llamacpp", "LlamaCppDriver"),
}


class InferenceBackend(CapabilityBackend):
    CAPABILITY = "inference"
    DRIVERS = DRIVERS
    ERROR_CLS = InferenceError

    def check_prerequisites(self, inference: Inference, nodes: Optional[List] = None) -> List[str]:
        return self.driver_for(inference).check_prerequisites(inference, nodes)

    @track_create("inference", name_of=lambda inference: inference.name)
    def create(self, inference: Inference, nodes: Optional[List] = None) -> str:
        return self.driver_for(inference).create(inference, nodes=nodes)

    @track_delete(name_of=lambda inference: inference.name)
    def delete(self, inference: Inference) -> None:
        return self.driver_for(inference).delete(inference)

    def endpoint(self, inference: Inference) -> str:
        return self.driver_for(inference).endpoint(inference)
