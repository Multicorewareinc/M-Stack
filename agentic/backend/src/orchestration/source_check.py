"""Checks the values a valid plan chose against the services that will run
them.

The SDK takes whatever a field's type allows; the service a component
deploys can take less, and only its own code says so. The tokenizer
service registers its encodings by name and won't start with any other
default, while the SDK's `default_encoding` is a plain string. So once the
SDK has accepted a plan, the values it chose -- fields with a default, set
to something else -- are looked up in that component's service code
(grounding.value_uses), and the model is asked one narrow question about
what it finds. A value is reported unsupported only with a line quoted
from that code, and the quote has to be there: a check that could invent
its evidence would refuse working plans.

Only fields with a default are checked. A required field holds the user's
own fact -- a name, a path, a Secret -- which no service code can
contradict; a credential never leaves the agent.

Before adding a check here for a validation gap found in the wild, triage
it first: is this a bug local to ONE capability's own Pydantic model (a
missing field_validator, a type too loose for what the real service
accepts), or a general pattern that shows up the same way across several
capabilities? A local bug belongs in that capability's own spec.py in the
SDK -- fixing it there catches every caller, not just this agent, and
doesn't grow this module for something that was never this layer's job.
A general pattern -- "the SDK's type allows more than the service does,"
which is what this whole module exists for -- earns a reusable mechanism
like this one, not a one-off special case bolted onto a single
capability's render function. request_check.py (same directory) is the
other general mechanism this layer has for the same reason: not "wrong
encoding gets its own patch," but "nothing checks a chosen value against
what the user actually said, for any field, ever" -- diagnosed once,
fixed once, for every capability at once.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import typing
from collections.abc import Callable, Sequence

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, SecretStr

from mcp_tools.architecture import PARAM_TO_SKILL
from mcp_tools.full_stack import PLACEHOLDER, WIRED_FIELDS
from orchestration.grounding import value_uses
from orchestration.selection import TOOL_FOR_SKILL, WHOLE_STACK_TOOL

logger = logging.getLogger(__name__)

_SKILL_FOR_TOOL = {tool: skill for skill, tool in TOOL_FOR_SKILL.items()}

INSTRUCTIONS = (
    "You check deployment settings against the code and docs of the service that will run "
    "them. Each numbered setting comes with that service's own material. Report a setting "
    "only when the material shows the service will not accept its value -- for example a "
    "registry, list or table of the values it supports that doesn't include it, or code that "
    "rejects it. A default, an example, or silence about the value is not evidence: report "
    "nothing then.\n"
    "Reply with JSON only, a list of "
    '{"setting": <number>, "quote": <the exact line from the material that shows it>, '
    '"supported": <the values the material says it accepts, or null>}. '
    "Reply [] when no setting is shown to be unsupported."
)

_ARRAY = re.compile(r"\[.*\]", re.S)

# On every component: `type` the SDK already checks against what it can
# deploy (SUPPORTED_TYPES), and `name`/`namespace` are the user's labels,
# not a choice among values a service supports.
_NOT_CHOICES = {"type", "name", "namespace"}


def _models(annotation) -> list[type[BaseModel]]:
    """The pydantic models inside Optional[...], Union[...] and List[...]."""
    if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
        return [annotation]
    return [m for arg in typing.get_args(annotation) for m in _models(arg)]


def _secret(annotation) -> bool:
    return annotation is SecretStr or any(_secret(a) for a in typing.get_args(annotation))


def _pick(models: Sequence[type[BaseModel]], value: dict) -> type[BaseModel] | None:
    """The model a dict was meant for: the first whose fields cover its keys."""
    for model in models:
        if set(value) <= set(model.model_fields):
            return model
    return models[0] if models else None


def _options_model(model: type[BaseModel], spec: dict) -> type[BaseModel] | None:
    """The options model `type` selects, as the SDK itself chooses it."""
    by_type = getattr(model, "OPTIONS_FOR_TYPE", None) or {}
    chosen = spec.get("type")
    if chosen is None and "type" in model.model_fields:
        chosen = model.model_fields["type"].get_default(call_default_factory=True)
    return by_type.get(chosen)


def _walk(model: type[BaseModel], spec: dict, capability: str, *, include_secrets: bool, path: tuple = ()):
    for name, value in spec.items():
        info = model.model_fields.get(name)
        if info is None or name in _NOT_CHOICES:
            continue
        nested = _models(info.annotation)
        if nested and isinstance(value, dict):
            sub = (_options_model(model, spec) if name == "options" else None) or _pick(
                nested, value
            )
            if sub is not None:
                yield from _walk(sub, value, capability, include_secrets=include_secrets, path=(*path, name))
            continue
        if info.is_required():
            continue
        if _secret(info.annotation) and not include_secrets:
            continue
        if name in WIRED_FIELDS or value == PLACEHOLDER:
            continue
        if not isinstance(value, (str, int, float, bool)):
            continue
        if value == info.get_default(call_default_factory=True):
            continue
        yield capability, name, str(value), (*path, name)


def chosen_values(
    function: Callable, tool_name: str, args: dict, *, include_secrets: bool = False
) -> list[tuple[str, str, str]]:
    """(capability, field, value) for each field with a default that the
    plan set to something else. chosen_fields() also says where each sits."""
    found: list[tuple[str, str, str]] = []
    for capability, name, value, _ in chosen_fields(function, tool_name, args, include_secrets=include_secrets):
        if (capability, name, value) not in found:
            found.append((capability, name, value))
    return found


def chosen_fields(
    function: Callable, tool_name: str, args: dict, *, include_secrets: bool = False
) -> list[tuple[str, str, str, tuple]]:
    """(capability, field, value, path) for each field with a default that
    the plan set to something else. `path` leads to the value in `args`:
    the tool parameter, an index for one of several, then the keys down.

    `include_secrets` is off by default for source_check's own use: no
    service code can "accept" or "reject" a credential, so there's
    nothing to look up. request_check.py passes True -- a credential the
    user never gave, chosen anyway, is exactly what it exists to catch;
    skipping secrets there would make the highest-value field invisible
    to the one check built to ask "did the user actually say this.\""""
    try:
        hints = typing.get_type_hints(inspect.unwrap(function))
    except Exception as e:  # an unreadable signature means nothing to check
        logger.warning("source check: can't read %s's parameters: %s", tool_name, e)
        return []
    found: list[tuple[str, str, str, tuple]] = []
    for param, value in args.items():
        models = _models(hints.get(param))
        if not models or value is None:
            continue
        capability = (
            PARAM_TO_SKILL.get(param, param)
            if tool_name == WHOLE_STACK_TOOL
            else _SKILL_FOR_TOOL.get(tool_name, param)
        )
        for index, spec in enumerate(value) if isinstance(value, list) else [(None, value)]:
            if isinstance(spec, dict):
                model = _pick(models, spec)
                if model is not None:
                    at = (param,) if index is None else (param, index)
                    found += _walk(model, spec, capability, include_secrets=include_secrets, path=at)
    return found


def _squash(text: str) -> str:
    return " ".join(text.split())


def _bare(text: str) -> str:
    """`text` with no whitespace at all: a quote of a multi-line definition
    comes back on one line, its spacing changed, its characters not."""
    return "".join(text.split())


def _material(n: int, use: dict) -> str:
    blocks = "\n\n".join(
        f"### {p['path']}:{p['start']}-{p['end']}\n({p['context']})\n{p['text']}"
        for p in use["passages"]
    )
    return f"Setting {n}: {use['capability']} {use['name']} = {use['value']!r}\n{blocks}"


def verify(uses: Sequence[dict], ask: Callable[[list], str]) -> list[dict]:
    """The settings `uses` shows unsupported: each use's dict plus "quote",
    "where" (the quoted passage's path:lines) and "supported". A quote that
    isn't in that setting's own material is dropped."""
    checked = [u for u in uses if u.get("passages")]
    if not checked:
        return []
    question = "\n\n".join(_material(n, u) for n, u in enumerate(checked, start=1))
    try:
        answer = ask([SystemMessage(content=INSTRUCTIONS), HumanMessage(content=question)])
        match = _ARRAY.search(answer or "")
        reported = json.loads(match.group(0)) if match else []
    except Exception as e:  # never worse off than without the check
        logger.warning("source check: skipped: %s", e)
        return []
    flags = []
    for item in reported if isinstance(reported, list) else []:
        try:
            use = checked[int(item["setting"]) - 1]
            quote = _squash(str(item["quote"]))
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        where = next(
            (
                f"{p['path']}:{p['start']}-{p['end']}"
                for p in use["passages"]
                if quote and _bare(quote) in _bare(p["text"])
            ),
            None,
        )
        if where is None:
            logger.info("source check: dropped a report whose quote isn't in the source: %r", quote)
            continue
        flags.append({**use, "quote": quote, "where": where, "supported": item.get("supported")})
    return flags


def message(flags: Sequence[dict]) -> str:
    """The error the model gets for a plan with unsupported values."""
    lines = []
    for f in flags:
        supported = f.get("supported")
        accepts = f" It accepts: {supported}." if supported else ""
        lines.append(
            f"the {f['capability']} service won't accept {f['name']}={f['value']!r}: "
            f"{f['where']} shows `{f['quote']}`.{accepts}"
        )
    return (
        " ".join(lines)
        + " Don't build it with that value: tell the user what the service supports and ask "
        "which to use."
    )


def check(
    function: Callable, tool_name: str, args: dict, ask: Callable[[list], str]
) -> tuple[str | None, list[dict]]:
    """(the error for the model, or None; the sources consulted)."""
    uses = value_uses(chosen_values(function, tool_name, args))
    sources = [
        {k: v for k, v in p.items() if k != "text"} for u in uses for p in u.get("passages", [])
    ]
    flags = verify(uses, ask)
    return (message(flags) if flags else None), sources
