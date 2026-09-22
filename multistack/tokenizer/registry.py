"""Which implementation serves which `type`, and the backend that dispatches."""
from __future__ import annotations

from typing import List

from ..capability import CapabilityBackend
from ..state.tracking import track_create, track_delete
from .spec import Tokenizer

DRIVERS = {
    "tiktoken": (
        "multistack.tokenizer.drivers.tiktoken_service",
        "TiktokenDriver",
    ),
}


class TokenizerBackend(CapabilityBackend):
    """Provisions the tokenizer, dispatching on the spec's `type`."""

    CAPABILITY = "tokenizer"
    DRIVERS = DRIVERS

    def check_prerequisites(self, tokenizer: Tokenizer) -> List[str]:
        return self.driver_for(tokenizer).check_prerequisites(tokenizer)

    @track_create("tokenizer", name_of=lambda tokenizer: tokenizer.type)
    def create(self, tokenizer: Tokenizer) -> str:
        return self.driver_for(tokenizer).create(tokenizer)

    @track_delete(name_of=lambda tokenizer: tokenizer.type)
    def delete(self, tokenizer: Tokenizer) -> None:
        return self.driver_for(tokenizer).delete(tokenizer)


__all__ = ["DRIVERS", "TokenizerBackend"]
