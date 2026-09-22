"""The tokenizer driver contract, and the errors a caller can catch."""
from __future__ import annotations

from typing import List, Protocol, runtime_checkable

from ..backends.transport import NodeCommandError, NodePrerequisiteError
from .spec import Tokenizer


class TokenizerError(NodeCommandError):
    """Something went wrong provisioning the tokenizer."""


class TokenizerPrerequisiteError(NodePrerequisiteError):
    """The cluster isn't ready: no helm on PATH, an unreachable API
    server. Distinct from `TokenizerError` because the fix is not in the
    spec."""


@runtime_checkable
class TokenizerDriver(Protocol):
    """What every tokenizer implementation must provide."""

    def check_prerequisites(self, tokenizer: Tokenizer) -> List[str]:
        """Returns warnings; raises on anything that would leave the
        tokenizer unusable."""
        ...

    def create(self, tokenizer: Tokenizer) -> str:
        """Installs the implementation and returns its endpoint."""
        ...

    def delete(self, tokenizer: Tokenizer) -> None:
        """Removes the installation."""
        ...


__all__ = ["TokenizerDriver", "TokenizerError", "TokenizerPrerequisiteError"]
