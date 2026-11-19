"""Shared by every resource's render function: the final self-check
before a generated script is ever handed back. Not a rendering
abstraction -- each resource's own shape (a nested options object, a
node list, cross-layer Stack wiring) differs enough that hand-writing
each render function directly reads clearer than forcing them through
one shared template."""

from __future__ import annotations

import ast
from collections.abc import Collection
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


def dumped_lines(model: BaseModel, dumped: dict[str, Any], *, skip: Collection[str] = ()) -> list[str]:
    """One rendered `"    field=value,"` line per entry in `dumped`
    (already dump()-ed), skipping any key in `skip` -- what every render
    function's own field_lines starts from.

    Fields the caller actually passed come first, as plain lines.
    Fields Pydantic fell back to its own class default for --
    model_fields_set is Pydantic's own record of which fields the caller
    really passed, not a guess -- are grouped after them, under one
    `# system-generated defaults` marker rather than repeating that
    comment on every such line. Lets whoever reads the generated script
    see at a glance which values came from the request and which the
    system chose on its own, without changing what the script does or
    what order keyword args matter in (none; these are all kwargs)."""
    explicit = []
    defaulted = []
    for k, v in dumped.items():
        if k in skip:
            continue
        line = f"    {k}={v!r},"
        (explicit if k in model.model_fields_set else defaulted).append(line)
    if not defaulted:
        return explicit
    return explicit + ["    # system-generated defaults -- not specified in the request"] + defaulted


def check_script(script: str) -> None:
    """Raises AssertionError if `script` isn't valid Python -- every
    render function calls this on its own output before returning,
    rather than trust string-building got it right."""
    try:
        ast.parse(script)
    except SyntaxError as e:
        raise AssertionError(f"generated script is invalid Python -- this is a bug in the template: {e}")
