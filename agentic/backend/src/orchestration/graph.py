"""
LangGraph orchestration -- the conductor. Ties the knowledge layer, the
model, and the MCP tool layer together, per the architecture decision
(docs/architecture-decision.md): the model reasons, tool calls are
validated as they're made with a bounded self-correction retry. This
agent's only job is to hand back a validated, readable script -- it
never provisions anything itself (see agentic/backend/src/mcp_server.py).

    NL request (skills and reference passages chosen before the graph
                runs -- see note below)
      -> call_model         (LLM decides a tool call, or writes text)
      -> execute_tools       (each call checked once for everything it needs)
           |-- text, nothing pending        --> END (answer or final reply)
           |-- text, empty                  --> back to call_model
           |-- a call the model can correct --> back to call_model
           |-- a call only the user can
           |   unblock: a choice            --> ask_choice (pauses with options)
           |   unblock: values              --> call_model words the question,
           |                                    this module checks it asks for
           |                                    every value and writes it itself
           |                                    when it doesn't --> END
           |-- a valid plan                 --> call_model writes the reply
           `-- turns exhausted              --> END (script, pending question,
                                                or a fallback message)

The model fills in the plan. What to ask the user, and whether as a list
of values or as a choice with options, is decided here from the tool's
own findings (orchestration/needs.py), never left to the model's wording.

An answer to a pause (ask_choice, confirm_selection, the model's own
ask_user tool) is treated like any new message from the user: skills and
tools are selected again for it and the turn budget starts over.

What the model reads is decided once per turn, before the graph runs,
in send_message()/stream_events(): orchestration/selection.py picks the
skills and tools the message needs, and, with RAG_ENABLED set,
orchestration/grounding.py reads from the RAG service
(api/microservices/rag) the SDK fields of every component the request
builds -- what each means, its default, its limits; the tool schema has
only names and types. Neither needs to be a graph node: both depend only
on the message, not on anything the graph produces. The passages ride in
the `retrieved` state field and are added to the latest user message
inside call_model, for that call only.

With RAG_ENABLED set, a plan the SDK accepted is also checked against the
services that will run it (orchestration/source_check.py): a value the
SDK takes but the service doesn't goes back to the model with the line
that says so.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import re
import typing
from pathlib import Path
from typing import AsyncIterable, Annotated, Iterable, Sequence, TypedDict

import anyio
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, interrupt
from pydantic import BaseModel, ValidationError

from llm import get_chat_model
from orchestration import grounding, needs, request_check, scope_check, source_check
from orchestration.grounding import field_docs, retrieve_for_rejection
from orchestration.knowledge import build_system_prompt
from orchestration.selection import chain, select, selection_ambiguity
from mcp_tools.full_stack import WIRED_FIELDS
from orchestration.tools import ALL_TOOLS

load_dotenv()

logger = logging.getLogger(__name__)

# Total call_model <-> execute_tools cycles allowed within ONE user turn --
# covers both self-correction (a failed validation retried) and simply
# needing more than one step (build, then write the final reply).
MAX_MODEL_TURNS = 4

_TOOLS_BY_NAME = {t.name: t for t in ALL_TOOLS}


def _format_tool_call_error(e: Exception) -> str:
    """Formats a tool-invocation failure into a clean message for the
    model to see and react to. A ValidationError means the tool's own
    Pydantic-based argument schema rejected the call (e.g. a bad type on
    a field) before the tool's code ever ran -- deliberately generic, not
    RKE2-specific, since any current or future tool's arguments can fail
    this way. Pydantic's own str() wraps this in a verbose blob (the raw
    input, a docs URL); this pulls out just "field: message" per
    underlying error instead. Anything else is reported as-is."""
    if not isinstance(e, ValidationError):
        return str(e)
    parts = []
    for err in e.errors():
        loc = ".".join(str(p) for p in err["loc"])
        msg = err["msg"].removeprefix("Value error, ")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts)


class OrchestrationState(TypedDict):
    """One conversation thread's state, checkpointed between calls."""

    # add_messages appends new messages to history instead of replacing
    # it -- without this reducer, each node's {"messages": [...]} return
    # would overwrite the whole conversation instead of extending it.
    messages: Annotated[list, add_messages]
    attempt: int
    reply: str | None  # the model's question, or its final text reply
    # The most recently generated script within THIS invocation. Unlike
    # reply/attempt, this is intentionally NOT reset on every execute_tools
    # pass -- it has to survive from the pass that built it through to the
    # later pass where the model writes its final text reply about it (see
    # execute_tools). send_message() resets it to None at the start of
    # each new invocation, so it never leaks across separate user turns.
    script: str | None
    # Which skills this thread has already been given, and which tools are
    # bound as a result. Accumulated rather than recomputed from scratch
    # each turn: "set its limit to 60" names no resource at all, and
    # without carrying the earlier turns' selection forward that follow-up
    # would arrive with no policy skill and no policy tool (see
    # orchestration/selection.py).
    skills: list[str]
    tools: list[str]
    # Which cluster this conversation is building onto, once a tool call has
    # named one -- restated to the model on every later turn (see
    # _seed_messages). Left to the history alone, a turn that loaded the
    # cluster-building rules asked "new cluster or existing?" two messages
    # after the user had given the kubeconfig.
    target: str | None
    # Reference passages retrieved for THIS turn's message (see
    # orchestration/grounding.py), spliced into the model's view of the
    # user's message in call_model and never stored in the history: a
    # later turn retrieves for its own message, and carrying every turn's
    # passages forward would fill the context window.
    retrieved: str
    # None unless selection itself was genuinely borderline this turn
    # (see orchestration/selection.py's selection_ambiguity()) -- when
    # set, confirm_selection() pauses the turn and asks the user, instead
    # of _rag_select_skills() silently deciding either way.
    ambiguity: dict | None
    # The values the last tool call still needs from the user, as
    # [{"path", "note"}], while the model words the question for them.
    # The reply is checked against this list before it goes out.
    asking: list | None
    # A choice the last tool call needs the user to make, and that call:
    # {"choice": {"key", "question", "options", "adds"}, "call": {"name",
    # "args"}}. ask_choice pauses on it.
    pending: dict | None
    # Whether a build tool has been called since the user last spoke, and
    # whether the model has already been told once to call one first.
    called: bool
    nudged: bool
    # Defaults the current script took where the SDK lists alternatives,
    # one line each, shown with the reply.
    assumed: list | None


_llm: dict[frozenset, object] = {}
_judge_llm = None

# For _ask_unbound only: these are judgement calls (which components, does
# a value match the service, does a value match the request) with one
# real answer per input, not prose where variety is fine. At the
# provider's own default (1.0 on vLLM for gpt-oss), the exact same input
# can get a different answer from one call to the next -- confirmed
# directly: the same request about a gateway's fail-open behavior
# correctly left a rate limiter out 4 times and composed one (then caught
# and asked about) twice, same prompt, same day. Low, not necessarily
# zero: vLLM's gpt-oss still benefits from a little room, zero sometimes
# degenerates on this server. call_model's own conversational replies are
# unaffected -- they still use _model()'s separately cached instance, at
# the configured or provider-default temperature.
JUDGEMENT_TEMPERATURE = 0.1


def _model(tool_names: Iterable[str] = ()):
    """The chat model with `tool_names` bound, cached per distinct set.

    Binding every tool would be simpler, but all fourteen schemas come to
    ~31k tokens and the model this runs against has a 32,768-token window,
    so a request carrying them plus its skills could not fit at all. The
    cache is keyed by the tool set rather than being a single instance,
    since different requests now bind different tools; get_chat_model()
    still runs once per distinct set rather than once per call.
    """
    key = frozenset(tool_names)
    if key not in _llm:
        selected = [t for t in ALL_TOOLS if t.name in key]
        # Empty stays a real, reachable case even though call_model()
        # itself always passes at least ["ask_user"] now: direct callers
        # and tests can still ask for no tools at all, and bind_tools([])
        # is rejected by some providers, so leave the model unbound then.
        model = get_chat_model()
        _llm[key] = model.bind_tools(selected) if selected else model
    return _llm[key]


async def call_model(state: OrchestrationState) -> dict:
    """Sends the full conversation so far to the model and appends its
    reply (a tool call, or plain text) to the message history.

    Logs before and after the actual model call -- this can be the
    slowest single step by far (a large system prompt + several tool
    schemas on a local/CPU-bound model can take real time to process),
    and a request that internally loops back for a retry (see
    execute_tools) produces no other visible output in between. Without
    this, a slow but working request and a genuinely hung one look
    identical from the caller's side -- confirmed in practice, not
    theoretical.

    `ainvoke`, not `invoke`: this is the one call in the whole turn slow
    enough to matter, and every provider's LangChain integration backs
    it with a real async HTTP client (confirmed for Ollama: langchain_ollama
    uses ollama.AsyncClient, not a thread-pooled wrapper around the sync
    call). Awaiting it here means a cancelled request -- the caller's
    connection dropped, e.g. the console's Stop button (see
    agentic/frontend/src/features/plan/PlanPage.tsx) -- actually aborts
    the in-flight HTTP call instead of leaving it to finish unread in an
    abandoned thread, which is what happened when this ran through
    `.invoke()` in a worker thread (see router.py's streaming routes)."""
    # ask_user is always bound, on top of whatever selection chose: the
    # model needs a way to signal genuine ambiguity about which of two
    # valid approaches to take on ANY turn, not only the skills selection
    # happened to pick for this one (see orchestration/tools.py).
    bound = [*(state.get("tools") or []), "ask_user"]
    logger.info(
        "call_model: attempt %d/%d, sending %d messages, %d tool(s) bound: %s",
        state["attempt"], MAX_MODEL_TURNS, len(state["messages"]), len(bound), ", ".join(bound),
    )
    response = await _model(bound).ainvoke(_with_reference(state["messages"], state.get("retrieved")))
    logger.info(
        "call_model: got a response (%s)",
        f"{len(response.tool_calls)} tool call(s)" if getattr(response, "tool_calls", None) else "text",
    )
    return {"messages": [response]}


def _judge_model():
    """The chat model used for judgement calls only (selection,
    source_check, request_check) -- see JUDGEMENT_TEMPERATURE. Cached
    separately from _model(): that cache is keyed by tool set and reused
    for the conversational loop at its own temperature, and this is
    deliberately a different instance, not a lower-temperature entry in
    the same cache, so the two can never be confused for each other."""
    global _judge_llm
    if _judge_llm is None:
        _judge_llm = get_chat_model(temperature=JUDGEMENT_TEMPERATURE)
    return _judge_llm


def _ask_unbound(messages: list) -> str:
    """The model's text answer to `messages`, with no tools bound -- every
    caller (selection.py, needs.py, source_check.py, request_check.py,
    scope_check.py) is a judgement call, so this uses _judge_model(), not
    the conversational loop's own instance.

    An empty answer is asked for once more. The model sometimes stops
    after its reasoning with nothing said (3 of 24 calls when measured),
    and every caller reads an empty judgement as "nothing to report"."""
    for attempt in (1, 2):
        text = _judge_model().invoke(messages).text or ""
        if text.strip():
            return text
        logger.info("judgement came back empty (attempt %d of 2)", attempt)
    return ""


def _format_attachment(content: str | None, name: str | None) -> str | None:
    """An attached file's content, labeled and fenced for the model to
    read -- fed into the same `retrieved` channel as RAG passages (see
    _combine_reference) rather than a channel of its own, since both are
    the same thing from call_model's point of view: extra context spliced
    onto the latest user message for this call only."""
    if not content:
        return None
    label = f"Attached file ({name})" if name else "Attached file"
    return f"{label}:\n```\n{content}\n```"


def _combine_reference(reference: str | None, attachment: str | None) -> str | None:
    """Merges retrieved reference passages with a formatted attachment
    into the one block `_with_reference` splices onto the latest user
    message. Either half may be absent."""
    parts = [p for p in (reference, attachment) if p]
    return "\n\n".join(parts) if parts else None


def _selection_text(message: str, attachment: str | None) -> str:
    """What select() (orchestration/selection.py) scans for trigger words,
    e.g. "valkey" or "cluster" -- the typed message plus an attachment's
    raw content, not the fenced/labeled version _format_attachment builds
    for the model. select() only does literal substring matching, so the
    label and fence markers would never match a trigger anyway; this is
    the same text, just for a different reader.

    Without this, a message that only says "see the attached file" (the
    console's own fallback when a file is sent with no text typed; see
    PlanPage.tsx's send()) selects zero skills and zero tools no matter
    what the file asks for -- the model can still read the attachment
    once it reaches call_model, but with nothing bound to call, it can
    never produce a script from it."""
    return f"{message}\n{attachment}" if attachment else message


def _with_reference(messages: list, reference: str | None) -> list:
    """`messages` with `reference` appended to the latest user message, for
    this call only. Unchanged when there is nothing to add."""
    if not reference:
        return messages
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            grounded = HumanMessage(content=f"{messages[i].content}\n\n{reference}")
            return [*messages[:i], grounded, *messages[i + 1:]]
    return messages


def _reference(message: str, skills: Sequence[str]) -> tuple[str, list[dict]]:
    """The SDK fields of what this message builds, the components it names
    first, then the rest of the thread's selection: a follow-up like "set
    its limit to 60" names nothing, and still needs the policy's fields."""
    built = chain(message)
    return field_docs(built + [s for s in skills if s not in built])


# How this module marks a message it wrote to the model itself. Such a
# message is not the user's words and is never read as if it were.
_AGENT_NOTE = "[Agent check, not from the user]"
# What an ask_user call gets as its result when it did not pause the turn:
# its question went out as an ordinary reply, or was held back until a
# build tool has been tried. Neither is something the user said.
_SHOWN_AS_REPLY = "Shown to the user as an ordinary question."
_NOT_ASKED = "Not asked: call the build tool first."


def _user_text(state: OrchestrationState) -> str:
    """Everything the user has said in this conversation: their messages,
    and their answers to the model's own ask_user questions."""
    parts = []
    for m in state["messages"]:
        if isinstance(m, HumanMessage):
            if not str(m.content).startswith(_AGENT_NOTE):
                parts.append(str(m.content))
        elif isinstance(m, ToolMessage) and m.name == "ask_user":
            try:
                answer = json.loads(m.content)
            except (TypeError, ValueError):
                answer = m.content
            if isinstance(answer, str) and answer not in (_SHOWN_AS_REPLY, _NOT_ASKED):
                parts.append(answer)
    return "\n".join(parts)


def _names_field(field: str, reply: str, *, other_fields: frozenset = frozenset()) -> bool:
    """Whether `reply` is asking about `field`, naming it either literally
    (`org_cp_internal_url`) or in the words a model actually tends to use
    instead (`"the organization control plane's internal URL"`) -- most
    of a snake_case field's own words, found anywhere in the reply, count
    as naming it. Matching only the literal identifier missed this in
    practice: the model paraphrased instead of naming the field, and the
    reply went to the user asking for something the tool already fills
    in. A short field (one or two words, e.g. `cache_url`) needs all of
    them, so a single common word can't match alone.

    `other_fields`, when given, guards against a real but different,
    longer field name being mistaken for `field` just because `field`'s
    words are a subset of it -- confirmed directly: a reply naming
    `cache_auth_url` (quoted, exact) was read as naming `cache_url`
    too, since "cache" and "url" both genuinely appear, and the wrong
    correction reached the model instead of the right one. If a longer
    name in `other_fields` contains every one of `field`'s own words AND
    is itself literally present in `reply`, that longer name is what's
    actually being discussed -- `field` is not also named."""
    if field in reply:
        return True
    words = field.split("_")
    lowered = reply.lower()
    for other in other_fields:
        if other == field:
            continue
        other_words = set(other.split("_"))
        if set(words) <= other_words and other in reply:
            return False
    hits = sum(1 for word in words if word in lowered)
    return hits >= max(2, len(words) - 1)


def _asks_for_wired_fields(reply: str, *, other_fields: frozenset = frozenset()) -> str | None:
    """A correction for a reply, sent before any plan was built, that asks
    the user for a field the whole-stack tool fills in itself; None
    otherwise."""
    wired = [field for field in WIRED_FIELDS if _names_field(field, reply, other_fields=other_fields)]
    if not wired:
        return None
    return (
        f"{', '.join(wired)} is filled in by the tool once the component it comes from is "
        "composed; never ask the user for it."
    )


def _nested_models(annotation) -> list[type[BaseModel]]:
    """The pydantic models inside Optional[...], Union[...] and List[...] --
    same resolution source_check.py's _models() does, kept local here
    rather than imported: tiny, and this module's own correction logic
    doesn't belong coupled to source_check's."""
    if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
        return [annotation]
    return [m for arg in typing.get_args(annotation) for m in _nested_models(arg)]


def _optional_model_field_names(model: type[BaseModel], *, _seen: set | None = None) -> set[str]:
    """Every field name declared anywhere in `model` or its own nested
    models (a tokenizer timeout living inside enricher.options, a cache's
    own auth URL), walked to full depth, that the SDK marks as NOT
    required. `_seen` guards against a model that nests itself (directly
    or through another) recursing forever."""
    _seen = _seen if _seen is not None else set()
    if model in _seen:
        return set()
    _seen.add(model)
    names: set[str] = set()
    for name, info in model.model_fields.items():
        if not info.is_required():
            names.add(name)
        for sub in _nested_models(info.annotation):
            names |= _optional_model_field_names(sub, _seen=_seen)
    return names


def _optional_sdk_field_names(tool_names: Sequence[str]) -> set[str]:
    """Every field name, anywhere in the real SDK models `tool_names`
    accept -- however deeply nested -- that the SDK itself marks as NOT
    required: it has a real default, so BASE_INSTRUCTIONS' own rule
    ("other fields keep their defaults") already says the model should
    never ask about it unprompted. Generalizes WIRED_FIELDS, which is
    really just the special case of a field that happens to also be
    auto-filled from another component -- both kinds share the one
    actual rule: if the SDK doesn't require it, the tool call names it
    as missing only when it's genuinely missing, and nothing else should
    prompt a question about it.

    Read from the real tool functions' own type hints, not a maintained
    list -- a new optional field anywhere in the SDK is covered the
    moment it's added, the same way request_check.py and source_check.py
    already read the real signature instead of hand-listing fields."""
    names: set[str] = set()
    for tool_name in tool_names:
        tool = _TOOLS_BY_NAME.get(tool_name)
        if tool is None:
            continue
        try:
            hints = typing.get_type_hints(inspect.unwrap(tool.func))
        except Exception as e:  # an unreadable signature means nothing to check
            logger.warning("execute_tools: can't read %s's parameters for optional fields: %s", tool_name, e)
            continue
        for hint in hints.values():
            for model in _nested_models(hint):
                names |= _optional_model_field_names(model)
    return names


def _known_fields(state: OrchestrationState) -> frozenset:
    """Every optional and wired field name of the bound tools, so a field
    whose words contain another's (cache_auth_url, cache_url) is told
    apart from it when a reply is read."""
    return frozenset(_optional_sdk_field_names(state.get("tools") or [])) | frozenset(WIRED_FIELDS)


# Text that asks the user for something, as opposed to telling them.
_ASKS = re.compile(
    r"\?|\b(?:please (?:provide|share|specify|tell|confirm|give)|could you|can you|let me know"
    r"|(?:i|we)(?:'ll| will)? need)\b",
    re.IGNORECASE,
)
_TRY_FIRST = (
    "If this is a request to build or change something, call the build tool now with what the "
    "user gave: it reports everything that is missing, and the user is then asked for exactly "
    "that. For a real choice between two ways to build it, call ask_user with the options. If "
    "the user only asked a question, answer it."
)
_SCRIPT_REPLY = (
    "Here is the validated deployment script. It passed the SDK's "
    "checks; review it before running it."
)


def _asks_before_trying(state: OrchestrationState, content: str) -> bool:
    """A question to the user on a turn where a build tool is on offer and
    none has been called yet. Asked that way, the question is the model's
    own guess at what is missing; after a call it is the tool's list."""
    return _untried(state) and _ASKS.search(content) is not None


def _untried(state: OrchestrationState) -> bool:
    """A build tool is on offer, none has been called since the user last
    spoke, and the model hasn't been told once already to call one."""
    return (
        bool(state.get("tools"))
        and not state.get("called")
        and not state.get("nudged")
        and not state.get("script")
        and state["attempt"] < MAX_MODEL_TURNS
    )


def _clean(text: str, state: OrchestrationState) -> str:
    return _mask_secrets(_CITATION_MARK.sub("", text), state["messages"])


def _finish(reply: str, state: OrchestrationState) -> str:
    """The reply, with what the script assumed and the script itself."""
    script = state.get("script")
    assumed = state.get("assumed")
    if script and assumed:
        reply = (
            f"{reply.rstrip()}\n\nAssumed, since the request didn't say: "
            + "; ".join(assumed)
            + ". Tell me to change any of these."
        )
    return _attach_script(reply, script)


def _question(state: OrchestrationState, content: str, asking: list) -> str:
    """The model's wording of the question for `asking`, made complete: a
    value it left out is added, and wording that can't go out at all (it
    asks for a wired field, or strays off topic) is replaced by the plain
    list."""
    plain = needs.render(asking)
    reply = _clean(content, state)
    if _asks_for_wired_fields(reply, other_fields=_known_fields(state)):
        return plain
    reply = scope_check.enforce(reply, _ask_unbound, fallback=plain)
    if reply == plain:
        return plain
    missing = needs.unasked(reply, asking)
    if missing:
        reply = f"{reply.rstrip()}\n\n" + needs.render(missing).replace(
            "To build this I still need:", "I also need:"
        )
    return reply


def _text_reply(state: OrchestrationState, content: str, closing: list) -> dict:
    """What happens to text from the model: held back for another try, or
    sent to the user. `closing` answers the tool call the text came in, if
    it came in one."""
    kept: dict = {"messages": list(closing)} if closing else {}
    if not content:
        # No tool call and no text: the model most likely ran out of its
        # token budget mid-reasoning. Another turn, not silence.
        return {**kept, "reply": None}

    asking = state.get("asking")
    if asking:
        return {**kept, "reply": _question(state, content, asking), "asking": None}

    if not state.get("script") and state["attempt"] < MAX_MODEL_TURNS:
        wired = _asks_for_wired_fields(content, other_fields=_known_fields(state))
        if wired:
            logger.info("execute_tools: reply held back -- %s", wired)
            # build_full_stack_plan by name: with single-resource tools
            # bound too, "the tool" read ambiguously and the model split
            # the request across them.
            note = (
                f"{_AGENT_NOTE} {wired} Call build_full_stack_plan now with everything the "
                "user gave -- don't split this into separate single-resource calls."
            )
            return {"reply": None, "messages": [*closing, HumanMessage(content=note)]}
        if _asks_before_trying(state, content):
            logger.info("execute_tools: reply held back -- a question before any tool call")
            return {
                "reply": None,
                "nudged": True,
                "messages": [*closing, HumanMessage(content=f"{_AGENT_NOTE} {_TRY_FIRST}")],
            }

    reply = scope_check.enforce(
        _clean(content, state), _ask_unbound, fallback=_SCRIPT_REPLY if state.get("script") else None
    )
    return {**kept, "reply": _finish(reply, state)}


def _build(tool, name: str, args: dict, state: OrchestrationState, said: str):
    """Runs a call that needs nothing more, then checks the plan it made
    against the services' own code (with retrieval on) and against what
    the user asked for."""
    try:
        result = tool.invoke(args)
    except GraphBubbleUp:
        raise
    except Exception as e:
        # The tool's own schema can still reject what needs.collect() let
        # through; the model gets the reason and another try.
        result = {"valid": False, "error": _format_tool_call_error(e)}
        reference = retrieve_for_rejection(result["error"], state.get("skills") or [])
        if reference:
            result["reference"] = reference
        return result
    if isinstance(result, dict) and result.get("valid") and grounding.enabled():
        unsupported, checked = source_check.check(tool.func, name, args, _ask_unbound)
        if unsupported:
            result = {"valid": False, "error": unsupported}
        if checked:
            result["checked"] = checked
    # The SDK proves a value is legal and source_check that the service
    # takes it; neither that it is the value the user asked for.
    if isinstance(result, dict) and result.get("valid"):
        mismatch = request_check.check(tool.func, name, args, said, _ask_unbound)
        if mismatch:
            result = {"valid": False, "error": mismatch}
    return result


def _pending(choice: needs.Choice, call: dict) -> dict:
    return {
        "choice": {
            "key": choice.key,
            "question": choice.question,
            "options": list(choice.options),
            "adds": {option: list(skills) for option, skills in choice.adds.items()},
        },
        "call": {"name": call["name"], "args": call["args"]},
    }


def _as_asking(values: Sequence[needs.Need]) -> list[dict]:
    return [{"path": n.path, "note": n.note} for n in values]


def _run_tools(state: OrchestrationState, tool_calls: list) -> dict:
    """Runs the model's tool calls. Each build call is checked once for
    everything it still needs (orchestration/needs.py); what the turn does
    next follows from that, not from how the model words a reply."""
    said = _user_text(state)
    skills = state.get("skills") or []
    update: dict = {"reply": None, "asking": None, "pending": None}
    messages: list = []
    asking: list[dict] = []
    pending = None
    called = bool(state.get("called"))
    # Whether everything blocking this turn is the user's to give. One
    # call the model has to correct sends the turn back to the model.
    user_only = True

    for call in tool_calls:
        name = call["name"]
        # DEBUG: arguments and results can be a whole cluster spec or a
        # rendered script. Without them a failed call shows only the
        # model's paraphrase of what went wrong.
        logger.debug("execute_tools: calling %s with args=%s", name, call["args"])
        tool = _TOOLS_BY_NAME.get(name)
        after: list = []
        if tool is None:
            result = {"error": f"unknown tool '{name}'"}
            user_only = False
        elif name == "ask_user":
            try:
                # Pauses here; on resume this returns the user's answer.
                result = str(tool.invoke(call["args"]))
            except GraphBubbleUp:
                raise
            except Exception as e:
                result = {"error": _format_tool_call_error(e)}
                user_only = False
            else:
                absorbed = _absorb_answer(state, result, options=call["args"].get("options") or ())
                after = [m for m in absorbed.pop("messages") if isinstance(m, SystemMessage)]
                update.update(absorbed)
                skills, said, called = update["skills"], f"{said}\n{result}", False
        else:
            called = True
            report = needs.collect(tool.func, name, call["args"], said, skills=skills, ask=_ask_unbound)
            if not report.blocked:
                result = _build(tool, name, report.args, state, said)
                if isinstance(result, dict) and result.get("valid"):
                    update["assumed"] = needs.assumed(tool.func, name, call["args"])
                else:
                    user_only = False
            else:
                result = {"valid": False, "error": report.message(), "needs": report.as_dict()}
                if report.fixes:
                    user_only = False
                    if report.sdk:
                        reference = retrieve_for_rejection(" ".join(report.sdk), skills)
                        if reference:
                            result["reference"] = reference
                elif report.choices:
                    pending = pending or _pending(report.choices[0], call)
                else:
                    asking += _as_asking(report.values)
        logger.debug("execute_tools: %s returned %s", name, result)

        if isinstance(result, dict) and result.get("valid") and result.get("script"):
            update["script"] = result["script"]
        elif isinstance(result, dict) and not result.get("valid"):
            # The script shown at the end of a turn is this turn's last
            # real outcome. A failed call abandons what an earlier call
            # built: "add monitoring" with no StorageClass once asked the
            # right question and attached an earlier attempt's bare Stack.
            update["script"] = None
            update["assumed"] = None
        target = _target_from(name, call["args"])
        if target:
            update["target"] = target

        messages.append(ToolMessage(content=json.dumps(result), tool_call_id=call["id"], name=name))
        messages += after

    update["messages"] = messages
    update["called"] = called
    if user_only and pending:
        update["pending"] = pending
    elif user_only and asking and not update.get("script"):
        update["asking"] = asking
    return update


def execute_tools(state: OrchestrationState) -> dict:
    """Acts on the model's last message: runs its tool calls, or treats
    its text as the reply. See route_after_tools for what follows."""
    last = state["messages"][-1]
    tool_calls = getattr(last, "tool_calls", None) or []
    if not tool_calls:
        # .text, not .content: a Responses-API model (see llm.py) returns
        # content as a list of reasoning and text blocks.
        return _text_reply(state, (last.text or "").strip(), [])
    if len(tool_calls) == 1 and tool_calls[0]["name"] == "ask_user":
        call = tool_calls[0]
        options = [o for o in call["args"].get("options") or [] if str(o).strip()]
        if len(options) < 2:
            # Without at least two options there is no choice to offer:
            # it is an ordinary question, and is handled as one, checked
            # like any other text before it reaches the user.
            closing = [ToolMessage(content=json.dumps(_SHOWN_AS_REPLY), tool_call_id=call["id"], name="ask_user")]
            return _text_reply(state, str(call["args"].get("question") or "").strip(), closing)
        if _untried(state):
            # A choice offered before any build tool was tried is held back
            # once, like a plain question: the tool's own findings decide
            # what is asked first, in wording whose answers are recognized.
            logger.info("execute_tools: ask_user held back -- no build tool called yet")
            return {
                "reply": None,
                "nudged": True,
                "messages": [
                    ToolMessage(content=json.dumps(_NOT_ASKED), tool_call_id=call["id"], name="ask_user"),
                    HumanMessage(content=f"{_AGENT_NOTE} {_TRY_FIRST}"),
                ],
            }
    return _run_tools(state, tool_calls)


def route_after_tools(state: OrchestrationState) -> str:
    """What follows execute_tools: stop with the reply in hand ("end"),
    pause on a choice the user has to make ("ask"), give the model another
    turn ("continue"), or wrap up once MAX_MODEL_TURNS is used
    ("budget_exhausted")."""
    if state.get("reply"):
        return "end"
    if state.get("pending"):
        return "ask"
    if state["attempt"] < MAX_MODEL_TURNS:
        return "continue"
    return "budget_exhausted"


def advance_turn(state: OrchestrationState) -> dict:
    """Counts one more call_model <-> execute_tools cycle against
    MAX_MODEL_TURNS before looping back to call_model."""
    return {"attempt": state["attempt"] + 1}


def budget_exhausted(state: OrchestrationState) -> dict:
    """Reached when MAX_MODEL_TURNS ran out before the model wrote a
    reply. The caller still gets the most useful thing in hand: the
    question the tool's findings call for, the script a tool validated
    on the last turn, or a request to clarify."""
    if state.get("asking"):
        return {"reply": needs.render(state["asking"]), "asking": None}
    if state.get("script"):
        return {"reply": _finish(_SCRIPT_REPLY, state)}
    return {
        "reply": (
            "I worked through what I could for this request but didn't reach a "
            "final script -- could you clarify what you're looking for?"
        )
    }


def _absorb_answer(
    state: OrchestrationState, answer: str, *, options: Sequence[str] = (), adds: Sequence[str] = ()
) -> dict:
    """The state update for what the user answered to a pause: selected,
    seeded and budgeted like any new message from them. `adds` are skills
    the chosen option brings in. An answer that is one of the offered
    options is selected for without a judgement call."""
    already = list(state.get("skills") or [])
    carried = [*already, *(s for s in adds if s not in already)]
    skills, tools = select(answer, carried, ask=None if answer in options else _ask_unbound)
    reference, _ = _reference(answer, skills)
    return {
        "messages": _seed_messages(answer, skills, already, new_thread=False, target=state.get("target")),
        "skills": skills,
        "tools": tools,
        "retrieved": reference,
        "attempt": 0,
        "called": False,
        "nudged": False,
        "asking": None,
        "pending": None,
    }


def ask_choice(state: OrchestrationState) -> dict:
    """Pauses on the choice a tool call needs the user to make, then works
    out what that call needs now that they have answered.

    Nothing runs before the pause, so resuming replays only the question."""
    choice, call = state["pending"]["choice"], state["pending"]["call"]
    answer = str(
        interrupt({"type": choice["key"], "question": choice["question"], "options": choice["options"]})
    )
    # What the paused call was building stays in the conversation whatever
    # the answer selects, along with what the chosen option brings in.
    update = _absorb_answer(
        state, answer, options=choice["options"],
        adds=[*needs.skills_of(call["name"], call["args"]), *(choice.get("adds") or {}).get(answer, ())],
    )
    tool = _TOOLS_BY_NAME.get(call["name"])
    if tool is None:
        return update
    report = needs.collect(
        tool.func, call["name"], call["args"], f"{_user_text(state)}\n{answer}",
        skills=update["skills"], ask=_ask_unbound,
    )
    if report.asks_user and report.choices:
        if report.choices[0].key == choice["key"] and answer not in choice["options"]:
            # They said something else instead of choosing, a question of
            # their own for instance. The model replies to that, and the
            # reply goes out as written; the choice comes up again when
            # the plan is next tried.
            update["nudged"] = True
            return update
        update["pending"] = _pending(report.choices[0], call)
    elif report.blocked:
        if report.asks_user:
            update["asking"] = _as_asking(report.values)
        update["messages"] = [*update["messages"], HumanMessage(content=f"{_AGENT_NOTE} {report.message()}")]
    return update


def route_after_choice(state: OrchestrationState) -> str:
    """Another choice to ask ("ask"), or back to the model ("continue")."""
    return "ask" if state.get("pending") else "continue"


def confirm_selection(state: OrchestrationState) -> dict:
    """Pauses the turn and asks the user, only when selection itself was
    genuinely borderline this turn (state["ambiguity"], set by
    send_message()/stream_events() from selection.py's
    selection_ambiguity()) -- a real, low-confidence fork, not a
    checklist of what was picked. Returns immediately, no pause, for the
    ordinary case where selection was clear either way.

    The human's answer comes back as whatever they typed next (the API
    layer resumes an interrupted thread with Command(resume=<their
    message>), same as any other reply) and is appended to the
    conversation as their own message, so call_model reads it exactly
    like any other turn -- no separate "confirmed" state to track."""
    ambiguity = state.get("ambiguity")
    if not ambiguity:
        return {}
    top = ambiguity["top_skill"]
    runner_up = ambiguity.get("runner_up")
    if runner_up:
        question = (
            f"This could mean either {top} or {runner_up} -- which did you want, "
            "or did you mean something else?"
        )
        options = [top, runner_up]
    else:
        question = f"Did you want {top} included here, or was that just mentioned in passing?"
        options = [f"yes, include {top}", f"no, leave {top} out"]
    answer = interrupt({"type": "selection_ambiguous", "question": question, "options": options})
    return {"messages": [HumanMessage(content=f"[Clarifying the request] {answer}")]}


def _checkpointer():
    """Where conversations are kept between messages: in memory, or in the
    SQLite file AGENTIC_STATE_DB names. On disk a restart keeps every
    conversation, a question still waiting for its answer included.

    AsyncSqliteSaver, not the sync SqliteSaver: every real graph run goes
    through ainvoke()/astream() now (send_message/stream_events), and the
    sync saver raises NotImplementedError on an async checkpoint read --
    confirmed by the aiosqlite package, already a transitive dependency,
    being installed. aiosqlite.connect() is lazy -- it returns a real
    Connection synchronously without actually opening the file yet, so
    this stays a plain, non-async function."""
    path = os.environ.get("AGENTIC_STATE_DB", "").strip()
    if not path:
        return MemorySaver()
    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    file = Path(path).expanduser()
    file.parent.mkdir(parents=True, exist_ok=True)
    return AsyncSqliteSaver(aiosqlite.connect(file))


def build_graph():
    """Wires up the nodes and edges described in the module docstring
    above, with a checkpointer so a thread's state survives between calls
    to send_message() -- and, with AGENTIC_STATE_DB set, between restarts
    (see _checkpointer)."""
    graph = StateGraph(OrchestrationState)
    graph.add_node("confirm_selection", confirm_selection)
    graph.add_node("call_model", call_model)
    graph.add_node("execute_tools", execute_tools)
    graph.add_node("ask_choice", ask_choice)
    graph.add_node("advance_turn", advance_turn)
    graph.add_node("budget_exhausted", budget_exhausted)

    graph.set_entry_point("confirm_selection")
    graph.add_edge("confirm_selection", "call_model")
    graph.add_edge("call_model", "execute_tools")
    graph.add_conditional_edges(
        "execute_tools",
        route_after_tools,
        {
            "continue": "advance_turn",
            "ask": "ask_choice",
            "budget_exhausted": "budget_exhausted",
            "end": END,
        },
    )
    graph.add_conditional_edges(
        "ask_choice", route_after_choice, {"ask": "ask_choice", "continue": "advance_turn"}
    )
    graph.add_edge("advance_turn", "call_model")
    graph.add_edge("budget_exhausted", END)

    return graph.compile(checkpointer=_checkpointer())


_GRAPH = None


def _graph():
    """Builds the compiled graph once per process and reuses it -- the
    graph itself is stateless; per-thread state lives in its checkpointer."""
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()
    return _GRAPH


def _snapshot(thread_id: str):
    """The checkpointer's current view of one thread, for a caller on a
    thread of its own (the sync FastAPI route, run off the event loop by
    Starlette's threadpool) -- never from inside send_message() or
    stream_events() themselves: with AGENTIC_STATE_DB set, the
    checkpointer is AsyncSqliteSaver, which refuses a synchronous read
    from the event-loop thread those two run on. _asnapshot() is their
    own counterpart, below."""
    config = {"configurable": {"thread_id": thread_id}}
    return _graph().get_state(config)


async def _asnapshot(thread_id: str):
    """Like _snapshot(), for a caller already on the event loop
    (send_message(), stream_events(), aget_state()) -- see _snapshot's
    own note for why the two can't share one function."""
    config = {"configurable": {"thread_id": thread_id}}
    return await _graph().aget_state(config)


def _state_from_snapshot(snapshot) -> dict:
    """A snapshot (from either _snapshot or _asnapshot) as the plain dict
    get_state()/aget_state() return: messages, reply, script, ..., and
    "ask_user" when the thread is currently paused on a human-in-the-loop
    question."""
    if not snapshot.interrupts:
        return snapshot.values
    payload = snapshot.interrupts[0].value
    return {
        **snapshot.values,
        "ask_user": {
            "question": payload.get("question", ""),
            "options": payload.get("options") or [],
        },
    }


def get_state(thread_id: str) -> dict:
    """Read-only snapshot of a thread's current state, without sending a
    message -- for the sync FastAPI route; see _snapshot's own note on
    why this stays synchronous. Empty dict for an unknown thread_id --
    callers checking whether a thread exists should look for that rather
    than catching an exception."""
    return _state_from_snapshot(_snapshot(thread_id))


async def aget_state(thread_id: str) -> dict:
    """Like get_state(), for a caller already on the event loop (tests
    driving the real graph, or a future async route) -- see _snapshot's
    own note."""
    return _state_from_snapshot(await _asnapshot(thread_id))


def _normalize_interrupt(result: dict) -> dict:
    """`result`, with LangGraph's own "__interrupt__" key (a tuple of
    Interrupt objects -- not JSON-serializable, not this module's own
    shape) replaced by a plain "ask_user" dict when the turn paused, or
    left untouched when it didn't. router.py's _to_response() reads
    "ask_user" directly, same as it reads "reply" or "script" -- it never
    needs to know LangGraph's own interrupt shape exists."""
    interrupts = result.get("__interrupt__")
    if not interrupts:
        return result
    payload = interrupts[0].value
    clean = {k: v for k, v in result.items() if k != "__interrupt__"}
    clean["ask_user"] = {
        "question": payload.get("question", ""),
        "options": payload.get("options") or [],
    }
    return clean


async def send_message(
    thread_id: str, message: str, *, attachment: str | None = None, attachment_name: str | None = None
) -> dict:
    """The one entry point for sending a message on a thread, whether
    it's brand new or already has history -- callers (the service layer
    in particular) don't need to track which. Seeds the system prompt
    automatically on a thread's first message; on any later message,
    explicitly resets the per-turn fields (attempt/reply/script) rather
    than leaving them at whatever a previous turn left them at --
    otherwise, for example, a turn that used up its model-turn budget
    would start the NEXT turn with none left, or a stale script from
    three turns ago would leak into a turn that never touched building
    one at all.

    Also decides which skills and tools this turn needs (see
    orchestration/selection.py). A brand-new thread gets BASE_INSTRUCTIONS
    plus whatever its first message selected. A later message that brings
    a new resource into the conversation ("now add a cache to that") gets
    the newly-selected skills appended as their own system message, rather
    than the thread being re-seeded -- the earlier ones are already in the
    history, and repeating them would both cost tokens and contradict
    nothing useful.

    `attachment` is a file's text content the caller attached to THIS
    message (the console's attach button; see router.py's MessageRequest)
    -- spliced onto the latest user message for this call only, the same
    way a retrieved reference passage is (see _with_reference). Not
    persisted: a later turn that wants the model to still see it needs to
    attach it again, same as re-stating anything else from earlier in the
    conversation.

    If the thread is currently paused at a human-in-the-loop question
    (confirm_selection, or the ask_user tool -- see orchestration/tools.py),
    `message` is treated as the human's answer, not a new turn: resumed
    with Command(resume=message), picking the graph back up exactly where
    it paused, same thread, same in-progress state."""
    config = {"configurable": {"thread_id": thread_id}}
    snapshot = await _asnapshot(thread_id)
    if snapshot.interrupts:
        return _normalize_interrupt(await _graph().ainvoke(Command(resume=message), config=config))

    new_thread = not snapshot.values.get("messages")
    previous = snapshot.values if not new_thread else {}
    already = previous.get("skills") or []
    skills, tools = await anyio.to_thread.run_sync(
        lambda: select(_selection_text(message, attachment), already, ask=_ask_unbound)
    )
    logger.info(
        "send_message: selected skills=[%s] tools=[%s]",
        ", ".join(skills) or "none", ", ".join(tools) or "none",
    )

    # _reference reads field docs through grounding.py, whose RAG lookup is a
    # sync httpx call; run it off the event loop rather than awaiting it
    # directly so it can't block other requests while this one waits.
    reference, _sources = await anyio.to_thread.run_sync(_reference, message, skills)
    update: OrchestrationState = {
        "messages": _seed_messages(
            message, skills, already, new_thread=new_thread, target=previous.get("target"),
        ),
        "attempt": 1,
        "reply": None,
        "script": None,
        "skills": skills,
        "tools": tools,
        "retrieved": _combine_reference(reference, _format_attachment(attachment, attachment_name)),
        "ambiguity": selection_ambiguity(message),
        "asking": None,
        "pending": None,
        "called": False,
        "nudged": False,
        "assumed": None,
    }
    return _normalize_interrupt(await _graph().ainvoke(update, config=config))


_CODE_BLOCK = re.compile(r"```[a-zA-Z]*\n.*?```", re.S)

# Some smaller models narrate their own tool calls by pasting the raw tool
# result back into their reply text, wrapped in a tag they invent
# themselves -- <tool_response>...</tool_response> is not something this
# service ever sends them, and not every provider does this, but it's
# confirmed in practice with qwen2.5:7b. That raw result can carry a
# generated credential (e.g. MinIO's root_password, RKE2's join token),
# so this is stripped at the source -- the API response, and everything
# persisted from it (agentic/frontend/src/lib/storage.ts saves every
# reply to localStorage), should never carry it in the first place. A
# frontend-only fix would still have leaked the credential into storage.
_TOOL_ECHO = re.compile(r"<tool_response>.*?</tool_response>\s*", re.S | re.I)


def _strip_tool_echo(text: str) -> str:
    return _TOOL_ECHO.sub("", text).strip()


# Fields whose value is itself a credential. A Secret's *name*
# (existing_secret, api_key_secret) is not one, and stays readable.
_CREDENTIAL_FIELDS = {
    "password", "cache_auth_url", "admin_cp_service_api_key", "access_key", "secret_key", "token",
}
MASK = "••••••"


def _credentials_given(messages: list) -> set[str]:
    """Every credential value passed to a tool in this conversation."""
    found: set[str] = set()

    def walk(value) -> None:
        if isinstance(value, dict):
            for key, inner in value.items():
                if key in _CREDENTIAL_FIELDS and isinstance(inner, str) and len(inner) >= 4:
                    found.add(inner)
                else:
                    walk(inner)
        elif isinstance(value, list):
            for inner in value:
                walk(inner)

    for message in messages:
        for call in getattr(message, "tool_calls", None) or []:
            walk(call.get("args", {}))
    return found


# gpt-oss marks sources the way its browsing tool does -- 【1†L1-L3】 --
# which here point at nothing the user can follow.
_CITATION_MARK = re.compile(r"\s?【[^】]{0,40}】")


# The characters a model swaps in when it typesets what it repeats: any of
# these dashes for a hyphen, any of these spaces for a space.
_DASHES = "-\u2010\u2011\u2012\u2013\u2014\u2212"
_SPACES = " \u00a0\u2009\u202f"


def _as_typeset(value: str) -> re.Pattern:
    """`value`, matched whichever dash or space each of its own was
    written with."""
    parts = []
    for char in value:
        if char in _DASHES:
            parts.append(f"[{re.escape(_DASHES)}]")
        elif char in _SPACES:
            parts.append(f"[{_SPACES}]")
        else:
            parts.append(re.escape(char))
    return re.compile("".join(parts))


def _mask_secrets(reply: str, messages: list) -> str:
    """The reply with any credential the user gave masked. Told not to
    repeat them, the model still listed a database password back in about
    half of live replies; the script keeps them, since it needs them.

    Matched as typeset, not only as given: a password with a hyphen came
    back written with non-breaking hyphens and went out unmasked."""
    for value in sorted(_credentials_given(messages), key=len, reverse=True):
        reply = _as_typeset(value).sub(MASK, reply)
    return reply


def _attach_script(reply: str, script: str | None) -> str:
    """The reply, with the tool's validated script attached exactly.

    Asked to present the script, the model rewrote it -- 146 changed lines
    against a 134-line script: a variable renamed, fields dropped, and every
    backend class left un-imported -- so the script a user would copy was
    not the one the SDK checked. Any code the model wrote is removed and the real
    script appended, so the reply can only ever carry validated code."""
    if not script:
        return reply
    prose = _CODE_BLOCK.sub("", reply).strip()
    return f"{prose}\n\n```python\n{script.rstrip()}\n```" if prose else f"```python\n{script.rstrip()}\n```"


def _target_from(tool: str, args: dict) -> str | None:
    """The cluster a tool call targets, as the model extracted it from the
    user's words -- recorded even when the call is then rejected, since the
    target is not what was wrong with it. The whole-stack tool's nested
    kubeconfig_path values are placeholders, so only its own count."""
    if tool == "build_full_stack_plan":
        if args.get("kubeconfig_path"):
            return f"the existing cluster at kubeconfig_path={args['kubeconfig_path']!r}"
        cluster = args.get("cluster")
        if isinstance(cluster, dict) and cluster.get("name"):
            return f"a new RKE2 cluster named {cluster['name']!r}"
        return None
    for value in args.values():
        if isinstance(value, dict) and value.get("kubeconfig_path"):
            return f"the existing cluster at kubeconfig_path={value['kubeconfig_path']!r}"
    return None


def _seed_messages(
    message: str, skills: list[str], already: list[str], *, new_thread: bool, target: str | None = None,
) -> list:
    """The messages one turn feeds into the graph -- shared by
    send_message() and stream_events() so the streamed and non-streamed
    paths can never disagree about how a turn's system prompt is seeded."""
    notes = [n for n in (_scope_note(skills), _target_note(target)) if n]
    turn = [HumanMessage(content="\n".join(notes) + f"\n\n{message}" if notes else message)]
    if new_thread:
        return [SystemMessage(content=build_system_prompt(skills)), *turn]
    added = [s for s in skills if s not in already]
    if added:
        # Only the newly relevant skills -- BASE_INSTRUCTIONS and the
        # earlier ones are already in this thread's history.
        return [SystemMessage(content=build_system_prompt(added, include_base=False)), *turn]
    return turn


def _target_note(target: str | None) -> str | None:
    if not target:
        return None
    return f"[Established earlier in this conversation: the target is {target}. Keep using it unless the user changes it.]"


def _scope_note(skills: list[str]) -> str | None:
    """Tells the model that this turn's own selection (see select(),
    already computed before this runs) names Multistack components. Left
    to the scope rule alone the model refused "what is rke2 cluster" as
    general knowledge, with the rke2 skill already in its prompt. Reuses
    the turn's real selection rather than a second, independent call --
    one judgement call per turn, not two agreeing with themselves.

    Goes inside the user's message rather than as its own system message:
    gpt-oss on vLLM mostly ignored a second system message (billing
    questions answered 1 time in 4), and answered every time with the
    note placed here."""
    if not skills:
        return None
    return (
        f"[This message names Multistack components ({', '.join(skills)}). "
        "Questions about them, including what they are, are in scope.]"
    )


async def stream_events(
    thread_id: str, message: str, *, attachment: str | None = None, attachment_name: str | None = None
) -> AsyncIterable[dict]:
    """Like send_message(), but yields progress AS the graph produces it,
    for a caller that wants to show what's happening while a multi-second,
    possibly multi-turn model call is in flight rather than only the
    finished result.

    Each item is one of:
        {"type": "tool_call",   "tool": <name>, "args": {...}}
        {"type": "reasoning",   "text": <str>}   -- only when the model
                                                    emitted text ALONGSIDE
                                                    a tool call; text with
                                                    no tool call is the
                                                    turn's real reply and
                                                    "final" already has it
        {"type": "tool_result", "tool": <name>, "valid": <bool|None>,
                                "error": <str|None>}
        {"type": "sources",     "sources": [{"path", "start", "end", ...}]}
                                -- only when retrieval is on and found
                                   something: the fields read before the
                                   first model call, and the service code
                                   a plan's values were checked against,
                                   just before that tool's result
        {"type": "ask_user",    "question": <str>, "options": [<str>, ...]}
                                -- the turn paused on a genuine
                                   human-in-the-loop question (borderline
                                   selection, or the model's own ask_user
                                   tool call -- see orchestration/tools.py);
                                   no "final" follows until the thread is
                                   resumed with the human's answer as its
                                   next message
        {"type": "final",       **<what send_message() returns>}

    "final" is always last, UNLESS the turn paused on "ask_user" instead --
    the two are mutually exclusive for a given call. Built on
    graph.stream()/graph.astream(stream_mode="updates") (a resumed turn
    still runs synchronously; a fresh one runs on the event loop so a
    client disconnect can cancel it), which yields one
    {node_name: state_delta} per node as it finishes, so this is a real
    trace of what ran: a step that never happened emits nothing, and a
    tool call's args/result here are exactly what execute_tools sent to
    and got back from the real tool.
    """
    config = {"configurable": {"thread_id": thread_id}}
    snapshot = await _asnapshot(thread_id)
    if snapshot.interrupts:
        async for chunk in _graph().astream(Command(resume=message), config=config, stream_mode="updates"):
            for event in _stream_chunk_events(chunk):
                yield event
        for event in _yield_final_or_ask(await _asnapshot(thread_id)):
            yield event
        return

    new_thread = not snapshot.values.get("messages")
    previous = snapshot.values if not new_thread else {}
    already = previous.get("skills") or []
    skills, tools = await anyio.to_thread.run_sync(
        lambda: select(_selection_text(message, attachment), already, ask=_ask_unbound)
    )
    logger.info(
        "stream_events: selected skills=[%s] tools=[%s]",
        ", ".join(skills) or "none", ", ".join(tools) or "none",
    )

    reference, sources = await anyio.to_thread.run_sync(_reference, message, skills)
    if sources:
        yield {"type": "sources", "sources": sources}
    update: OrchestrationState = {
        "messages": _seed_messages(
            message, skills, already, new_thread=new_thread, target=previous.get("target"),
        ),
        "attempt": 1,
        "reply": None,
        "script": None,
        "skills": skills,
        "tools": tools,
        "retrieved": _combine_reference(reference, _format_attachment(attachment, attachment_name)),
        "ambiguity": selection_ambiguity(message),
        "asking": None,
        "pending": None,
        "called": False,
        "nudged": False,
        "assumed": None,
    }

    async for chunk in _graph().astream(update, config=config, stream_mode="updates"):
        for event in _stream_chunk_events(chunk):
            yield event

    for event in _yield_final_or_ask(await _asnapshot(thread_id)):
        yield event


def _stream_chunk_events(chunk: dict) -> Iterable[dict]:
    """One graph.stream(stream_mode="updates") chunk, translated into the
    public event shapes stream_events() documents -- shared by a fresh
    turn and a resumed one, so the two can never describe the same chunk
    shape differently.

    A chunk keyed "__interrupt__" is LangGraph's own signal that the turn
    just paused on a human-in-the-loop question (confirm_selection, or
    the model's own ask_user tool call) -- translated to "ask_user" here
    so that internal shape never reaches a caller directly."""
    if "__interrupt__" in chunk:
        payload = chunk["__interrupt__"][0].value
        yield {
            "type": "ask_user",
            "question": payload.get("question", ""),
            "options": payload.get("options") or [],
        }
        return
    for node, delta in chunk.items():
        if node == "call_model":
            msg = (delta.get("messages") or [None])[0]
            if msg is None:
                continue
            calls = getattr(msg, "tool_calls", None) or []
            for call in calls:
                yield {"type": "tool_call", "tool": call["name"], "args": call["args"]}
            text = (msg.text or "").strip()
            if text and calls:
                yield {"type": "reasoning", "text": text}
        elif node == "execute_tools":
            for tool_message in delta.get("messages") or []:
                # Only a build tool's outcome is a result to show: not a
                # note to the model, and not the user's answer to ask_user.
                if not isinstance(tool_message, ToolMessage) or tool_message.name == "ask_user":
                    continue
                try:
                    result = json.loads(tool_message.content)
                except (TypeError, ValueError):
                    result = {}
                if isinstance(result, dict) and result.get("checked"):
                    yield {"type": "sources", "sources": result["checked"]}
                yield {
                    "type": "tool_result",
                    "tool": tool_message.name,
                    "valid": result.get("valid") if isinstance(result, dict) else None,
                    "error": result.get("error") if isinstance(result, dict) else None,
                    "looked_up": isinstance(result, dict) and bool(result.get("reference")),
                    "needs": result.get("needs") if isinstance(result, dict) else None,
                }


def _yield_final_or_ask(snapshot) -> Iterable[dict]:
    """After a stream loop ends: "final", the same shape send_message()
    returns, or nothing at all when the turn paused instead -- its own
    "ask_user" event already came from _stream_chunk_events() as the
    pause happened, so there's nothing further to add until the thread
    is resumed with the human's answer. Takes the snapshot already fetched
    by the caller (_asnapshot, on stream_events()'s own event-loop
    thread) rather than fetching its own -- see _snapshot's own note."""
    if snapshot.interrupts:
        return
    yield {"type": "final", **snapshot.values}
