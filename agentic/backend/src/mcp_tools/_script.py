"""Shared by every resource's render function: the final self-check
before a generated script is ever handed back. Not a rendering
abstraction -- each resource's own shape (a nested options object, a
node list, cross-layer Stack wiring) differs enough that hand-writing
each render function directly reads clearer than forcing them through
one shared template."""

from __future__ import annotations

import ast
from typing import Any

from pydantic import BaseModel, SecretStr


def dump(model: BaseModel) -> dict[str, Any]:
    """model.model_dump() with every SecretStr, at any depth, replaced by
    its real value -- what every render function builds its field lines
    from.

    A plain model_dump() keeps SecretStr objects, and repr() of one is
    the literal SecretStr('**********'): valid Python, so check_script()
    passes, and the service then authenticates with asterisks. For a
    rate limiter's cache credential that means failing open. Unwrapping
    here, once, rather than per field in each tool is what keeps a
    secret field the SDK adds later from shipping masked."""
    return _reveal(model.model_dump())


def _reveal(value: Any) -> Any:
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, dict):
        return {k: _reveal(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_reveal(v) for v in value]
    return value


def check_script(script: str) -> None:
    """Raises AssertionError if `script` isn't valid Python -- every
    render function calls this on its own output before returning,
    rather than trust string-building got it right."""
    try:
        ast.parse(script)
    except SyntaxError as e:
        raise AssertionError(f"generated script is invalid Python -- this is a bug in the template: {e}")
