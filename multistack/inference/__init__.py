"""The inference capability. Re-exports only — drivers stay private."""
from .base import InferenceDriver, InferenceError, InferencePrerequisiteError
from .registry import DRIVERS, InferenceBackend
from .spec import DEFAULT_MODEL, Inference, LlamaCppOptions, VLLMOptions

__all__ = [
    "Inference", "VLLMOptions", "LlamaCppOptions", "DEFAULT_MODEL",
    "InferenceBackend",
    "InferenceDriver", "InferenceError", "InferencePrerequisiteError",
    "DRIVERS",
]
