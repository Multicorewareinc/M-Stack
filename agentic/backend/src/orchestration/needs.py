"""What a tool call still needs before it can become a script, as data.

One pass over a call's arguments reports everything at once: values only
the user can give, choices the user has to make, and things the model can
correct itself. graph.py decides what happens next from that report, so
the model never has to work out on its own what to ask the user.

Every finding comes from the call's own inputs: the SDK types' validation
errors, the dependency table and the rules the whole-stack tool already
applies. Nothing here names a particular request.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import typing
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import ValidationError

from mcp_tools.architecture import DEPENDS_ON, PARAM_TO_SKILL
from mcp_tools.full_stack import (
    WIRED_FIELDS,
    against_the_user,
    invented_fields,
    missing_parts,
    pointless_policy,
    prepare_args,
)
from orchestration.knowledge import field_options, must_ask
from orchestration.selection import (
    WHOLE_STACK_TOOL,
    cluster_choice_given,
    onto_existing_cluster,
    wants_new_cluster,
)
from orchestration.source_check import _SKILL_FOR_TOOL, _models

logger = logging.getLogger(__name__)

NEW_CLUSTER = "Create a new cluster"
EXISTING_CLUSTER = "Use an existing cluster"


@dataclass(frozen=True)
class Need:
    """A value only the user can give."""

    path: str
    note: str = ""


@dataclass(frozen=True)
class Choice:
    """A decision the user has to make between fixed options."""

    key: str
    question: str
    options: tuple[str, ...]
    # option -> skills that option brings into the conversation
    adds: dict = field(default_factory=dict, compare=False)


CLUSTER_CHOICE = Choice(
    "cluster",
    "Do you want to create a new Kubernetes cluster, or use an existing one?",
    (NEW_CLUSTER, EXISTING_CLUSTER),
    {NEW_CLUSTER: ("rke2-cluster",)},
)


@dataclass
class Report:
    """What one tool call needs. `args` is the call as it should be sent
    to the tool: blank strings dropped, wired fields filled in."""

    args: dict
    values: list[Need] = field(default_factory=list)
    choices: list[Choice] = field(default_factory=list)
    fixes: list[str] = field(default_factory=list)
    # Those of `fixes` that are the SDK types' own rejections.
    sdk: list[str] = field(default_factory=list)
    # Values a fix will make necessary, found by applying it on a copy.
    after_fix: list[Need] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return bool(self.values or self.choices or self.fixes)

    @property
    def asks_user(self) -> bool:
        """Nothing left for the model to correct: only the user can unblock it."""
        return not self.fixes and bool(self.values or self.choices)

    def message(self) -> str:
        """The rejection the model reads."""
        if self.fixes:
            text = " ".join(self.fixes)
            pending = _unique([*self.values, *self.after_fix])
            if pending:
                text += (
                    " With that corrected, these still need the user's own value: "
                    + listed(pending)
                    + ". Pass each one the user already gave; the user will be asked for the rest."
                )
            return text
        if self.choices:
            choice = self.choices[0]
            return (
                f"The user is being asked: {choice.question} ({' / '.join(choice.options)}). "
                "Their answer follows."
            )
        return ask_instruction(self.values)

    def as_dict(self) -> dict:
        return {
            "values": [n.path for n in self.values],
            "choices": [c.key for c in self.choices],
            "fixes": len(self.fixes),
        }


def listed(needs: Sequence[Need]) -> str:
    return "; ".join(f"`{n.path}`" + (f" ({n.note})" if n.note else "") for n in needs)


def ask_instruction(needs: Sequence[Need]) -> str:
    """Tells the model to phrase the question; what it must contain is fixed."""
    return (
        "Everything else is in place. These need the user's own value: "
        + listed(needs)
        + ". Ask the user for exactly these, all in one message, naming each field as written "
        "here and saying in a few words what it is. Ask for nothing else. If the user already "
        "gave one of them, call the tool again with it instead of asking."
    )


def render(needs: Sequence[dict | Need]) -> str:
    """The question itself, written without the model. Used whenever the
    model's own wording can't be trusted to have asked for everything."""
    lines = ["To build this I still need:"]
    for need in needs:
        path, note = (need.path, need.note) if isinstance(need, Need) else (need["path"], need.get("note", ""))
        lines.append(f"- `{path}`" + (f": {note}" if note else ""))
    return "\n".join(lines)


def unasked(reply: str, needs: Sequence[dict]) -> list[dict]:
    """Those of `needs` that `reply` doesn't ask for."""
    return [need for need in needs if not _mentions(need["path"], reply)]


def _mentions(path: str, reply: str) -> bool:
    """Whether `reply` names the field at `path`: literally, or in the
    words of its name ("the cluster's StorageClass" for
    existing_storage_class, "what limit" for limits)."""
    lowered = reply.lower().replace("-", " ")
    squeezed = re.sub(r"[\s_`]", "", lowered)
    if path.lower() in lowered:
        return True
    leaf = re.sub(r"\[\d+\]", "", path.split(".")[-1]).lower()
    if leaf in lowered or leaf.replace("_", "") in squeezed:
        return True
    words = [w for w in leaf.split("_") if w]
    hits = sum(1 for w in words if w in squeezed or w.rstrip("s") in squeezed)
    return hits >= max(1, len(words) - 1)


def _unique(needs: Sequence[Need]) -> list[Need]:
    seen: dict[str, Need] = {}
    for need in needs:
        if need.path not in seen or (need.note and not seen[need.path].note):
            seen[need.path] = need
    return list(seen.values())


def without_blanks(value):
    """`value` with empty strings removed at any depth: a field passed as
    "" was not given, and is reported as missing like any other."""
    if isinstance(value, dict):
        return {k: without_blanks(v) for k, v in value.items() if not (isinstance(v, str) and not v.strip())}
    if isinstance(value, list):
        return [without_blanks(v) for v in value]
    return value


def _skill(tool_name: str, param: str) -> str:
    """The skill a tool parameter belongs to."""
    if tool_name == WHOLE_STACK_TOOL:
        return PARAM_TO_SKILL.get(param, param)
    return _SKILL_FOR_TOOL.get(tool_name, param)


def skills_of(tool_name: str, args: dict) -> list[str]:
    """The skills of the components a call composes."""
    if tool_name != WHOLE_STACK_TOOL:
        return [_SKILL_FOR_TOOL[tool_name]] if tool_name in _SKILL_FOR_TOOL else []
    return [
        PARAM_TO_SKILL.get(param, param)
        for param, value in args.items()
        if value is not None and (param in DEPENDS_ON or param == "cluster")
    ]


_FIELD_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")


def _path(param: str, loc: Sequence) -> str:
    """A pydantic error location as `component.field[0].sub`, without the
    type names pydantic adds for each branch of a union."""
    path = param
    for part in loc:
        if isinstance(part, int):
            path += f"[{part}]"
        elif _FIELD_NAME.match(str(part)):
            path += f".{part}"
    return path


def _target(args: dict, said: str) -> str | None:
    """Which cluster a whole-stack call builds onto: "new", "existing", or
    None while neither the call nor the user has said."""
    if args.get("cluster") is not None:
        return "new"
    if args.get("kubeconfig_path") is not None:
        return "existing"
    if wants_new_cluster(said):
        return "new"
    return "existing" if onto_existing_cluster(said) else None


def new_cluster_on_existing(args: dict, said: str) -> str | None:
    """A plan that builds a new cluster, or block storage for one, when the
    user gave the kubeconfig of the cluster they have, as the error to
    return, or None. In evaluation the model often composed `cluster` there,
    was told cluster.name was missing, and asked the user for a name and
    nodes of a cluster nobody wanted."""
    if not onto_existing_cluster(said):
        return None
    extra = [name for name in ("cluster", "storage") if args.get(name)]
    if not extra:
        return None
    return (
        "the user's cluster already exists -- they gave its kubeconfig. Pass it as "
        f"kubeconfig_path and leave {' and '.join(extra)} out: they build a new cluster. "
        "Components that need storage take existing_storage_class on an existing cluster. "
        "Call again; don't ask the user about a new cluster."
    )


def _with_only_types(function: Callable, args: dict) -> dict:
    """`args` with a required `type` filled in wherever the SDK supports
    exactly one: there is nothing to choose, so nothing to ask or retry."""
    try:
        hints = typing.get_type_hints(inspect.unwrap(function))
    except Exception:
        return args
    filled = dict(args)
    for param, value in args.items():
        models = list(dict.fromkeys(_models(hints.get(param))))
        if len(models) != 1:
            continue
        options = list(getattr(models[0], "OPTIONS_FOR_TYPE", None) or ())
        info = models[0].model_fields.get("type")
        if len(options) != 1 or info is None or not info.is_required():
            continue
        if isinstance(value, dict) and "type" not in value:
            filled[param] = {**value, "type": options[0]}
        elif isinstance(value, list):
            filled[param] = [
                {**item, "type": options[0]} if isinstance(item, dict) and "type" not in item else item
                for item in value
            ]
    return filled


def as_sent(function: Callable, tool_name: str, args: dict) -> dict:
    """`args` as the tool receives them from the agent: blank strings
    dropped, a single-option type filled in and, for the whole-stack tool,
    the fields it wires itself."""
    args = _with_only_types(function, without_blanks(args))
    return prepare_args(args) if tool_name == WHOLE_STACK_TOOL else args


def _errors(model, item) -> list[dict]:
    try:
        model.model_validate(item)
    except ValidationError as e:
        return e.errors()
    return []


def _at(model, item, loc: Sequence):
    """The nested model and the arguments given for it at `loc`, or
    (None, None) when `loc` doesn't lead through plain fields to one."""
    for part in loc:
        info = model.model_fields.get(part) if isinstance(part, str) else None
        nested = list(dict.fromkeys(_models(info.annotation))) if info else []
        if len(nested) != 1:
            return None, None
        model = nested[0]
        item = item.get(part) if isinstance(item, dict) else None
    return model, item


# A validator saying that something has to be there, as opposed to one
# saying that two values don't go together.
_DEMANDS = re.compile(
    r"\b(?:required|at least one|must be (?:set|given|provided|specified)|is missing|needs? (?:an?|at least))\b",
    re.IGNORECASE,
)


def _demanded_field(model, item, message: str) -> str | None:
    """The field a validator's message asks for, when that field was not
    given: "At least one RKE2 node is required" is about `nodes`. Such a
    rejection is a value the user has to give, like any missing field."""
    if not _DEMANDS.search(message):
        return None
    words = set(re.findall(r"[a-z0-9]+", message.lower()))
    for name in model.model_fields:
        if isinstance(item, dict) and item.get(name) not in (None, "", [], {}):
            continue
        if all(part in words or part.rstrip("s") in words for part in name.split("_")):
            return name
    return None


def _sentinel(annotation):
    """A stand-in of the right shape for a field that is missing, so that
    validation can run past it."""
    origin = typing.get_origin(annotation)
    if origin in (list, set, tuple):
        return []
    if origin is typing.Union:
        return _sentinel(next(a for a in typing.get_args(annotation) if a is not type(None)))
    if _models(annotation) or origin is dict:
        return {}
    return {int: 1, float: 1.0, bool: False}.get(annotation, "needed")


def _behind_missing(model, item) -> list[tuple[tuple, str]]:
    """Rejections that only show once the missing fields are there: (where,
    message). The SDK's own validators don't run while a field is missing,
    so "at least one node" stays hidden behind "name is required"."""
    import copy

    probe = copy.deepcopy(item) if isinstance(item, dict) else item
    for _ in range(3):
        missing = [
            e["loc"] for e in _errors(model, probe)
            if e["type"] == "missing" and all(isinstance(part, str) for part in e["loc"])
        ]
        if not missing:
            break
        for loc in missing:
            owner, target = _at(model, probe, loc[:-1])
            if owner is None or not isinstance(target, dict) or loc[-1] not in owner.model_fields:
                return []
            target[loc[-1]] = _sentinel(owner.model_fields[loc[-1]].annotation)
    return [
        (tuple(e["loc"]), e["msg"].removeprefix("Value error, "))
        for e in _errors(model, probe)
        if e["type"] != "missing"
    ]


def _sdk_findings(function: Callable, tool_name: str, args: dict) -> tuple[list[Need], list[str]]:
    """What the SDK types themselves reject in `args`: values the user has
    to give (a required field that is missing, or one a validator demands),
    and everything else as text for the model."""
    try:
        hints = typing.get_type_hints(inspect.unwrap(function))
    except Exception as e:  # an unreadable signature: the tool reports it
        logger.warning("needs: can't read %s's parameters: %s", tool_name, e)
        return [], []
    values: list[Need] = []
    fixes: list[str] = []
    for param, value in args.items():
        models = list(dict.fromkeys(_models(hints.get(param))))
        if len(models) != 1 or value is None:
            continue
        model = models[0]
        items = value if isinstance(value, list) else [value]
        for index, item in enumerate(items):
            label = f"{param}[{index}]" if isinstance(value, list) and len(items) > 1 else param

            def demanded(loc: Sequence, message: str) -> Need | None:
                owner, given = _at(model, item, loc)
                name = _demanded_field(owner, given, message) if owner is not None else None
                if name is None:
                    return None
                return Need(f"{_path(label, loc)}.{name}", message[:1].lower() + message[1:].rstrip("."))

            errors = _errors(model, item)
            for error in errors:
                message = error["msg"].removeprefix("Value error, ")
                if error["type"] == "missing":
                    values.append(Need(_path(label, error["loc"])))
                    continue
                need = demanded(error["loc"], message)
                if need:
                    values.append(need)
                else:
                    fixes.append(f"{_path(label, error['loc'])}: {message.rstrip('.')}.")
            if any(error["type"] == "missing" for error in errors):
                # Only what turns out to be a value to give is taken from
                # behind the missing fields; anything else there may be
                # about the stand-ins, and shows for real on the next call.
                values += filter(None, (demanded(loc, message) for loc, message in _behind_missing(model, item)))
    return _unique(values), list(dict.fromkeys(fixes))


_STATED_INSTRUCTIONS = (
    "You are given what a user wrote, a decision, and the options for it. Reply with JSON only: "
    "the list of those options the user's own words ask for, exactly as spelled in the list. "
    "Count an option only when the user's words name it or name the thing it stands for. Do not "
    "infer it from what is usual, from a number, or from the rest of the request: when the words "
    "would fit more than one option, the user has not said which, and the answer is []."
)
_ARRAY = re.compile(r"\[.*\]", re.S)
_stated_memo: dict[tuple, tuple[str, ...]] = {}


def stated_options(
    said: str,
    options: Sequence[str],
    ask: Callable | None,
    *,
    about: str = "",
    wordings: dict | None = None,
) -> tuple[str, ...] | None:
    """Which of `options` the user's own words ask for. An option written
    out by name, or in one of the `wordings` its skill declares for it,
    counts without a model call; otherwise one narrow judgement call reads
    the text. None when there is no usable judgement: the caller then
    treats the choice as not made, since asking once too often costs a
    click and assuming costs a wrong plan."""
    lowered = said.lower().replace("-", " ")

    def written(option: str) -> bool:
        if re.search(rf"(?<![a-z0-9]){re.escape(option.lower())}(?![a-z0-9])", lowered):
            return True
        return any(phrase.replace("-", " ") in lowered for phrase in (wordings or {}).get(option, ()))

    literal = tuple(o for o in options if written(o))
    if literal:
        return literal
    if ask is None:
        return None
    key = (said, tuple(options), about)
    if key in _stated_memo:
        return _stated_memo[key]
    question = (
        f'What the user wrote:\n"""{said}"""\n\n'
        + (f"The decision: {about}\n" if about else "")
        + f"Options: {json.dumps(list(options))}"
    )
    try:
        answer = ask([SystemMessage(content=_STATED_INSTRUCTIONS), HumanMessage(content=question)])
    except Exception as e:
        logger.warning("needs: stated-option judgement failed: %s", e)
        return None
    match = _ARRAY.search(answer or "")
    if not match:
        logger.warning("needs: stated-option judgement unreadable: %r", (answer or "")[:80])
        return None
    # The names inside the brackets, however they were quoted.
    listed = match.group(0).lower()
    result = tuple(o for o in options if re.search(rf"(?<![a-z0-9]){re.escape(o.lower())}(?![a-z0-9])", listed))
    if len(_stated_memo) > 256:
        _stated_memo.clear()
    _stated_memo[key] = result
    return result


def _must_ask_findings(
    function: Callable, tool_name: str, args: dict, said: str, ask: Callable | None
) -> tuple[list[Choice], list[str]]:
    """Choices a skill says the user has to make themselves, when the user
    hasn't: asked as a choice. A plan that contradicts what they did say is
    a correction for the model."""
    flagged = must_ask()
    if not flagged:
        return [], []
    try:
        hints = typing.get_type_hints(inspect.unwrap(function))
    except Exception:
        return [], []
    choices: list[Choice] = []
    fixes: list[str] = []
    for param, value in args.items():
        skill = _skill(tool_name, param)
        if skill not in flagged or value is None:
            continue
        name, question, wordings = flagged[skill]
        models = list(dict.fromkeys(_models(hints.get(param))))
        if len(models) != 1 or name != "type":
            continue
        options = tuple(getattr(models[0], "OPTIONS_FOR_TYPE", None) or ())
        info = models[0].model_fields.get(name)
        if len(options) < 2 or info is None:
            continue
        stated = stated_options(
            said, options, ask, about=question or f"the {skill} {name}", wordings=wordings
        )
        if not stated:
            choices.append(Choice(f"{param}.{name}", question or f"Which {skill} {name} do you want?", options))
            continue
        default = info.get_default(call_default_factory=True)
        specs = [s for s in (value if isinstance(value, list) else [value]) if isinstance(s, dict)]
        planned = [str(s.get(name, default)) for s in specs]
        wrong = sorted({p for p in planned if p not in stated})
        if wrong:
            fixes.append(
                f"{param}.{name}={wrong[0]!r} is not what the user asked for: they asked for "
                f"{' and '.join(stated)}. Set {param}.{name} to match."
            )
    return choices, fixes


def _settings_of(model, item, name: str, where: str):
    """Every place under `item` where the field `name` can be set: (where,
    the value given or None, its default)."""
    if not isinstance(item, dict):
        return
    info = model.model_fields.get(name)
    if info is not None and not info.is_required():
        yield where, item.get(name), info.get_default(call_default_factory=True)
    for key, value in item.items():
        inner = model.model_fields.get(key)
        nested = list(dict.fromkeys(_models(inner.annotation))) if inner else []
        if len(nested) != 1:
            continue
        if isinstance(value, dict):
            yield from _settings_of(nested[0], value, name, f"{where}.{key}")
        elif isinstance(value, list):
            for index, element in enumerate(value):
                yield from _settings_of(nested[0], element, name, f"{where}.{key}[{index}]")


def _named(option: str, lowered: str) -> bool:
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(option.lower())}(?![a-z0-9])", lowered))


def _named_but_unused(function: Callable, tool_name: str, args: dict, said: str) -> list[str]:
    """Corrections for a field left at its default while the user's words
    name another of its options: the plan would build the default without
    a word. Seen live on 2026-10-13, when "a server node and two agent
    nodes" came back as three servers because no node was given a `role`.

    The options are the SDK's own for a component's `type`, and a skill's
    `options:` line for a field the SDK checks without listing. A field
    passed explicitly is the model's decision and is left to it, so the
    correction is never made twice."""
    try:
        hints = typing.get_type_hints(inspect.unwrap(function))
    except Exception:
        return []
    listed_by_skill = field_options()
    asked = {skill for skill, flag in must_ask().items() if flag[0] == "type"}
    lowered = said.lower()
    fixes: list[str] = []
    for param, value in args.items():
        models = list(dict.fromkeys(_models(hints.get(param))))
        if len(models) != 1 or value is None:
            continue
        model, skill = models[0], _skill(tool_name, param)
        fields = dict(listed_by_skill.get(skill, {}))
        types = tuple(getattr(model, "OPTIONS_FOR_TYPE", None) or ())
        if len(types) > 1 and skill not in asked:
            fields.setdefault("type", types)
        items = value if isinstance(value, list) else [value]
        for name, options in fields.items():
            found = [
                setting
                for index, item in enumerate(items)
                for setting in _settings_of(model, item, name, f"{param}[{index}]" if len(items) > 1 else param)
            ]
            unset = [where for where, given, _ in found if given is None]
            if not unset:
                continue
            default = found[0][2]
            used = {str(default if given is None else given) for _, given, _ in found}
            missing = [o for o in options if o not in used and _named(o, lowered)]
            if not missing:
                continue
            named = " and ".join(f"`{o}`" for o in missing)
            if len(found) == 1:
                fixes.append(
                    f"`{unset[0]}.{name}` is left at its default {default!r}, but the user's words name {named}: "
                    f"pass `{name}` there as the request says."
                )
            else:
                fixes.append(
                    f"`{name}` is left at its default {default!r} on {len(unset)} of the {len(found)} entries under "
                    f"`{param}` ({', '.join(unset[:4])}), but the user's words name {named}: pass `{name}` on "
                    "each entry as the request says."
                )
    return fixes


def assumed(function: Callable, tool_name: str, args: dict) -> list[str]:
    """One line per choice a valid plan left at its default where the SDK
    itself lists alternatives, for the reply to show."""
    try:
        hints = typing.get_type_hints(inspect.unwrap(function))
    except Exception:
        return []
    asked = {skill for skill, flag in must_ask().items() if flag[0] == "type"}
    lines = []
    for param, value in args.items():
        models = list(dict.fromkeys(_models(hints.get(param))))
        if len(models) != 1 or value is None or _skill(tool_name, param) in asked:
            continue
        options = list(getattr(models[0], "OPTIONS_FOR_TYPE", None) or ())
        info = models[0].model_fields.get("type")
        if len(options) < 2 or info is None or info.is_required():
            continue
        default = info.get_default(call_default_factory=True)
        for spec in value if isinstance(value, list) else [value]:
            if isinstance(spec, dict) and "type" not in spec:
                others = ", ".join(f"`{o}`" for o in options if o != default)
                lines.append(f"{param} type `{default}` (alternatives: {others})")
                break
    return lines


def collect(
    function: Callable,
    tool_name: str,
    args: dict,
    said: str,
    *,
    skills: Sequence[str] = (),
    ask: Callable | None = None,
    _preview: bool = False,
) -> Report:
    """Everything `args` still needs before `tool_name` can build from it.

    `said` is the user's own side of the conversation; `skills` what this
    conversation selected; `ask` the judgement-call model, used only for
    a choice a skill says the user must make themselves."""
    args = _with_only_types(function, without_blanks(args))
    whole_stack = tool_name == WHOLE_STACK_TOOL
    report = Report(args=prepare_args(args) if whole_stack else args)

    if whole_stack:
        pointless = pointless_policy(args)
        if pointless and "policy" not in skills:
            # Nobody asked for rate limiting: the only thing to say is to
            # drop it. Its dependencies would bury that under requirements
            # of a component that shouldn't be there.
            report.fixes.append(pointless)
            return report
        if pointless:
            report.values.append(Need("policy.limits", "the limit to enforce, for example per user or per model"))
        for fix in (new_cluster_on_existing(args, said), against_the_user(args, said)):
            if fix:
                report.fixes.append(fix)
        components, storage = missing_parts(args)
        # Which storage is missing follows the cluster: one a new cluster
        # composes, or the class an existing one already has. With the
        # cluster itself still undecided, that question comes first.
        target = _target(args, said)
        if storage and target == "existing":
            report.values.append(Need("existing_storage_class", "the name of a StorageClass the cluster already has"))
        elif storage and target == "new":
            components = [*components, "storage"]
        if components:
            named = ", ".join(c if c != "controlplane" else 'controlplane (type="organization")' for c in components)
            report.fixes.append(
                f"The plan leaves out what its components depend on: add {named} and call again."
            )
            if not _preview:
                projected = {
                    **args,
                    **{c: ({"type": "organization"} if c == "controlplane" else {}) for c in components},
                }
                report.after_fix = collect(function, tool_name, projected, said, skills=skills, _preview=True).values

    values, report.sdk = _sdk_findings(function, tool_name, report.args)
    report.values += values
    report.fixes += report.sdk

    if whole_stack:
        # A wired field is only ever missing while the component it comes
        # from isn't composed yet, and that is a dependency to add, never
        # a value to ask the user for.
        report.values = [n for n in report.values if n.path.rsplit(".", 1)[-1] not in WIRED_FIELDS]
        report.values += [
            Need(path, why if why.startswith("left out") else "the real value: a stand-in was passed")
            for path, why in invented_fields(report.args)
        ]
        target_missing = args.get("cluster") is None and args.get("kubeconfig_path") is None
        target_path = "kubeconfig_path"
    else:
        target = [n for n in report.values if n.path.endswith(".kubeconfig_path") and n.path.count(".") == 1]
        target_missing = bool(target)
        target_path = target[0].path if target else ""
        report.values = [n for n in report.values if n not in target]

    if target_missing:
        if not cluster_choice_given(said):
            report.choices.append(CLUSTER_CHOICE)
        elif wants_new_cluster(said):
            report.fixes.append(
                "The user wants a new cluster: pass `cluster` with its name and nodes."
                if whole_stack
                else "The user wants a new cluster: build this with build_full_stack_plan, "
                f"passing `cluster` and this component, not with {tool_name}."
            )
            if whole_stack and not _preview:
                report.after_fix += collect(
                    function, tool_name, {**args, "cluster": {}}, said, skills=skills, _preview=True
                ).values
        else:
            report.values.append(Need(target_path, "the path to the existing cluster's kubeconfig file"))

    if not _preview:
        choices, fixes = _must_ask_findings(function, tool_name, args, said, ask)
        report.choices += choices
        report.fixes += fixes
        report.fixes += _named_but_unused(function, tool_name, args, said)

    report.values = _unique(report.values)
    report.after_fix = [n for n in _unique(report.after_fix) if n.path not in {v.path for v in report.values}]
    return report
