"""Checks the values a valid plan chose against what the user actually asked
for, in their own words.

The SDK proves a value is legal. source_check.py proves the real service
accepts it. Neither proves it's the value the user typed: both would pass
`max_inflight=1000` just as happily as `max_inflight=200`, whichever one
the model chose, because both are legal integers no service code rejects.
Catching that gap needs the user's own request, not more SDK or service
rules.

Once a plan is otherwise valid, every field whose value differs from its
default (source_check.chosen_values -- the same set that check already
computes) is shown next to the user's own request text, and the model is
asked one narrow question: does this value genuinely match what the user
said, or does it contradict or invent something they never stated? This
is deliberately NOT literal or string matching -- "200" appearing
somewhere in the request proves nothing about which field it belongs to,
and a value can be correct despite being worded nothing like it ("on port
nine thousand and ninety" is 9090). Only a real judgement call can read
that, the same reason source_check.py asks a question instead of grepping
for a number.

A value the whole-stack tool wired in from another component never
reaches this check at all (chosen_values excludes WIRED_FIELDS), and
neither does one the script comes out the same without: the tool takes a
component's storage class from the cluster's, whatever the plan passes
there, and "queue.storage_class doesn't match the request" for a value
that changes nothing once stopped a correct plan and had the model ask
the user which component the StorageClass was for. Among
what does: a field the user never mentioned is reported only if the
value itself has no ordinary justification -- ordinary, unremarkable
choices the user didn't spell out are not a mismatch, but a setting with
no basis in the request AND nothing ordinary about its value is exactly
what this exists to catch. A retrieved passage can describe a field's
shape in detail without the user ever having asked for a specific value
in it; that description is not the user's own words, and a value that
traces only to it, not to anything the user said, is reported the same
as one that outright contradicts them.

include_secrets=True on every lookup here (unlike source_check's own):
a credential the user never gave, chosen anyway, is the highest-value
thing this check exists to catch, and source_check's reason for skipping
secrets -- no service code can contradict one -- has nothing to do with
whether the user actually said it.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from collections.abc import Callable, Sequence

from langchain_core.messages import HumanMessage, SystemMessage

from orchestration.source_check import chosen_fields

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "You check whether the values a deployment plan chose genuinely match what the user actually "
    "asked for, quoted below in their own words. Every setting shown already differs from its "
    "field's own default -- something set it there on purpose, and your job is to tell whether "
    "that purpose traces back to the user's own words. Report a setting when its value contradicts "
    "what the user said, or when the user's words say nothing to justify it at all and the value "
    "itself is not an ordinary, unremarkable choice -- an extreme, unusual or suspicious value with "
    "nothing in the request behind it is exactly what to report, even with no direct contradiction "
    "to quote. Do not report a setting just because it's phrased differently from the user's words "
    "(a port given as 'nine thousand and ninety' matches 9090), or because it's a small, ordinary "
    "adjustment nobody would need to spell out. Reply with JSON only, a list of "
    '{"setting": <number>, "why": <one sentence saying what the user said that this value '
    "contradicts, or why it has no basis in the request if nothing justifies it>}. Reply [] when "
    "every setting is either stated by the user or an ordinary, unremarkable choice."
)

_ARRAY = re.compile(r"\[.*\]", re.S)


def _material(n: int, item: tuple[str, str, str]) -> str:
    capability, name, value = item
    return f"Setting {n}: {capability} {name} = {value!r}"


def verify(
    chosen: Sequence[tuple[str, str, str]], user_text: str, ask: Callable[[list], str]
) -> list[dict]:
    """The settings `chosen` shows don't match `user_text`: each as
    {"capability", "name", "value", "why"}. A report whose "setting"
    number isn't a real index is dropped, same discipline as
    source_check.verify() drops a quote that isn't in its own material."""
    if not chosen or not user_text.strip():
        return []
    question = f'User\'s request, in their own words:\n"""{user_text}"""\n\n' + "\n".join(
        _material(n, item) for n, item in enumerate(chosen, start=1)
    )
    try:
        answer = ask([SystemMessage(content=INSTRUCTIONS), HumanMessage(content=question)])
        match = _ARRAY.search(answer or "")
        reported = json.loads(match.group(0)) if match else []
    except Exception as e:  # never worse off than without the check
        logger.warning("request check: skipped: %s", e)
        return []
    flags = []
    for item in reported if isinstance(reported, list) else []:
        try:
            capability, name, value = chosen[int(item["setting"]) - 1]
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        flags.append(
            {
                "capability": capability,
                "name": name,
                "value": value,
                "why": str(item.get("why", "")).strip(),
            }
        )
    return flags


def message(flags: Sequence[dict]) -> str:
    """The error the model gets for a plan whose values don't match the
    user's own request."""
    lines = [
        f"{f['capability']}.{f['name']}={f['value']!r} doesn't match what the user asked: {f['why']}"
        for f in flags
    ]
    return (
        " ".join(lines)
        + " Re-read the user's request and fix the value(s) that don't match it, or ask them to "
        "confirm which they meant if it's genuinely ambiguous."
    )


def _script(function: Callable, args: dict) -> str | None:
    """The script `args` builds, or None when they build none."""
    try:
        result = function(**args)
    except Exception:
        return None
    return result.get("script") if isinstance(result, dict) and result.get("valid") else None


def _without(args: dict, path: Sequence) -> dict:
    """`args` with the value at `path` taken out."""
    out = copy.deepcopy(args)
    target = out
    for key in path[:-1]:
        target = target[key]
    del target[path[-1]]
    return out


def reaching_the_script(function: Callable, args: dict, chosen: Sequence[tuple]) -> list[tuple]:
    """Those of `chosen` (source_check.chosen_fields) whose value changes
    the script. One the tool replaces with its own wiring builds the same
    script with or without it, so there is nothing of the user's to hold
    it against. All of them when the plan can't be rendered to compare."""
    script = _script(function, args)
    if script is None:
        return list(chosen)
    kept = []
    for item in chosen:
        try:
            unchanged = _script(function, _without(args, item[3])) == script
        except (KeyError, IndexError, TypeError):
            unchanged = False
        if not unchanged:
            kept.append(item)
    return kept


def check(
    function: Callable, tool_name: str, args: dict, user_text: str, ask: Callable[[list], str]
) -> str | None:
    """The error for the model, or None when every chosen value matches
    what the user asked for (or there was nothing to check).

    include_secrets=True: a credential the user never gave, chosen anyway,
    is exactly the gap this module exists to close -- source_check skips
    secrets because no service code can contradict one, but that
    reasoning doesn't carry over here."""
    chosen = chosen_fields(function, tool_name, args, include_secrets=True)
    if not chosen or not user_text.strip():
        return None
    shown = list(dict.fromkeys(item[:3] for item in reaching_the_script(function, args, chosen)))
    flags = verify(shown, user_text, ask)
    return message(flags) if flags else None
