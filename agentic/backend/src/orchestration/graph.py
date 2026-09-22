"""
LangGraph orchestration -- the conductor. Ties the knowledge layer, the
model, and the MCP tool layer together, per the architecture decision
(docs/architecture-decision.md): the model reasons, tool calls are
validated as they're made with a bounded self-correction retry. This
agent's only job is to hand back a validated, readable script -- it
never provisions anything itself (see agentic/backend/src/mcp_server.py).

    NL request (knowledge already loaded into the system prompt --
                see note below)
      -> call_model          (LLM decides a tool call, or asks a question)
      -> execute_tools       (Tool layer: each call validated immediately)
           |-- no tool calls, real text     --> END (question or final reply)
           |-- no tool calls, empty         --> back to call_model (retry --
           |                                    likely ran out of token budget)
           |-- tool(s) ran (pass or fail),
           |   turns remain                 --> back to call_model
           `-- turns exhausted, nothing
               resolved                     --> END (fallback message)

Looping back to call_model after a tool call isn't only for retrying a
validation failure -- it's also how the model gets a second turn to
write the final reply that presents the script it just built, instead
of the raw tool result being the only thing the caller sees.

Knowledge loading isn't its own graph node today: build_system_prompt()
just concatenates every SKILL.md unconditionally (see knowledge.py), so
there's nothing request-dependent to run inside the graph. That changes
-- and this becomes a real node -- once the knowledge layer moves to
RAG (per the architecture decision), where retrieval genuinely depends
on the request and needs to run as its own step.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Iterable, Annotated, TypedDict

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from pydantic import ValidationError

from llm import get_chat_model
from orchestration.knowledge import build_system_prompt
from orchestration.selection import select, select_skills
from mcp_tools.full_stack import prepare_args as prepare_full_stack_args
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


_llm: dict[frozenset, object] = {}


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
        # No tools is a legitimate outcome -- a purely conversational turn
        # ("what can you do") needs none, and bind_tools([]) is rejected by
        # some providers, so leave the model unbound in that case.
        model = get_chat_model()
        _llm[key] = model.bind_tools(selected) if selected else model
    return _llm[key]


def call_model(state: OrchestrationState) -> dict:
    """Sends the full conversation so far to the model and appends its
    reply (a tool call, or plain text) to the message history.

    Logs before and after the actual model call -- this can be the
    slowest single step by far (a large system prompt + several tool
    schemas on a local/CPU-bound model can take real time to process),
    and a request that internally loops back for a retry (see
    execute_tools) produces no other visible output in between. Without
    this, a slow but working request and a genuinely hung one look
    identical from the caller's side -- confirmed in practice, not
    theoretical."""
    logger.info(
        "call_model: attempt %d/%d, sending %d messages, %d tool(s) bound: %s",
        state["attempt"], MAX_MODEL_TURNS, len(state["messages"]),
        len(state.get("tools") or []), ", ".join(state.get("tools") or []) or "none",
    )
    response = _model(state.get("tools") or []).invoke(state["messages"])
    logger.info(
        "call_model: got a response (%s)",
        f"{len(response.tool_calls)} tool call(s)" if getattr(response, "tool_calls", None) else "text",
    )
    return {"messages": [response]}


def execute_tools(state: OrchestrationState) -> dict:
    """Runs whatever tool call(s) the model's last message requested (or,
    if it made none, treats the message as a question/final reply for the
    caller). See route_after_tools for what happens with the result."""
    last = state["messages"][-1]
    tool_calls = getattr(last, "tool_calls", None) or []

    if not tool_calls:
        # .text, not .content: a Responses-API model (see llm.py) returns
        # content as a list of reasoning and text blocks, and .text is the
        # reply text alone for either shape.
        content = (last.text or "").strip()

        if not content:
            # No tool call AND no visible text -- the model likely ran out
            # of its token budget mid-reasoning before producing anything
            # (confirmed in practice with a reasoning model at too low a
            # max_tokens; see agentic/backend/src/llm.py). Loop back for another turn
            # instead of leaving the caller with silence.
            return {"reply": None}

        # A real question or a final text reply -- stop here. `script` is
        # deliberately left untouched (see OrchestrationState) so a script
        # built on an earlier pass of this same invocation is still there
        # for the caller once the model's closing reply arrives.
        return {"reply": _attach_script(content, state.get("script"))}

    tool_messages = []
    update: dict = {"reply": None}

    for call in tool_calls:
        # DEBUG, not INFO -- a tool call's arguments and result can be
        # large (a whole cluster spec, a rendered script) and this is
        # specifically for diagnosing *why* a call failed or a weaker
        # model isn't retrying well, not routine output. Without this,
        # only the model's own paraphrase of what went wrong is visible
        # -- confirmed in practice not reliable enough on its own to
        # diagnose a real failure.
        logger.debug("execute_tools: calling %s with args=%s", call["name"], call["args"])
        tool = _TOOLS_BY_NAME.get(call["name"])
        if tool is None:
            result = {"error": f"unknown tool '{call['name']}'"}
        else:
            args = call["args"]
            if call["name"] == "build_full_stack_plan":
                args = prepare_full_stack_args(args)
            try:
                result = tool.invoke(args)
            except Exception as e:
                # A tool's own argument schema (Pydantic-based) can reject
                # a call before the tool's code ever runs -- e.g. a bad
                # type on a field. Without this, that raises straight out
                # of execute_tools() and crashes the whole graph
                # invocation instead of giving the model a chance to see
                # what was wrong and retry, same as any other validation
                # failure a tool reports through its normal return value.
                result = {"valid": False, "error": _format_tool_call_error(e)}
        logger.debug("execute_tools: %s returned %s", call["name"], result)

        if isinstance(result, dict) and result.get("valid") and result.get("script"):
            update["script"] = result["script"]
        target = _target_from(call["name"], call["args"])
        if target:
            update["target"] = target

        tool_messages.append(
            ToolMessage(content=json.dumps(result), tool_call_id=call["id"], name=call["name"])
        )

    update["messages"] = tool_messages
    return update


def route_after_tools(state: OrchestrationState) -> str:
    """Decides what happens after execute_tools() runs: stop with a
    question/reply already in hand ("end"), give the model another turn
    ("continue"), or give up gracefully if MAX_MODEL_TURNS is used up
    ("budget_exhausted")."""
    if state.get("reply"):
        return "end"
    if state["attempt"] < MAX_MODEL_TURNS:
        return "continue"
    return "budget_exhausted"


def advance_turn(state: OrchestrationState) -> dict:
    """Counts one more call_model <-> execute_tools cycle against
    MAX_MODEL_TURNS before looping back to call_model."""
    return {"attempt": state["attempt"] + 1}


def budget_exhausted(state: OrchestrationState) -> dict:
    """Safety net, not the expected path: reached only if MAX_MODEL_TURNS
    ran out without the model ever asking a question or reaching a final
    reply -- e.g. it kept calling build/render without ever concluding.
    Guarantees the caller gets a real message back instead of an empty
    response.

    When a tool did return a valid script -- on the model's last allowed
    turn, so it never got to write about it -- that script is the answer.
    Saying no script was reached, while one sat in state, told the user a
    working plan had failed."""
    if state.get("script"):
        return {
            "reply": _attach_script(
                "Here is the validated deployment script. It passed the SDK's "
                "checks; review it before running it.",
                state["script"],
            )
        }
    return {
        "reply": (
            "I worked through what I could for this request but didn't reach a "
            "final script -- could you clarify what you're looking for?"
        )
    }


def build_graph():
    """Wires up the nodes and edges described in the module docstring
    above, with an in-memory checkpointer (MemorySaver) so a thread's
    state survives between calls to send_message(). In-memory means
    process-local: a restart loses every thread (see
    docs/architecture-decision.md Phase D for the real fix)."""
    graph = StateGraph(OrchestrationState)
    graph.add_node("call_model", call_model)
    graph.add_node("execute_tools", execute_tools)
    graph.add_node("advance_turn", advance_turn)
    graph.add_node("budget_exhausted", budget_exhausted)

    graph.set_entry_point("call_model")
    graph.add_edge("call_model", "execute_tools")
    graph.add_conditional_edges(
        "execute_tools",
        route_after_tools,
        {
            "continue": "advance_turn",
            "budget_exhausted": "budget_exhausted",
            "end": END,
        },
    )
    graph.add_edge("advance_turn", "call_model")
    graph.add_edge("budget_exhausted", END)

    return graph.compile(checkpointer=MemorySaver())


_GRAPH = None


def _graph():
    """Builds the compiled graph once per process and reuses it -- the
    graph itself is stateless; per-thread state lives in its checkpointer."""
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()
    return _GRAPH


def _snapshot(thread_id: str):
    """The checkpointer's current view of one thread -- shared by every
    read-only helper below so they don't each rebuild the same config
    dict and re-fetch it separately."""
    config = {"configurable": {"thread_id": thread_id}}
    return _graph().get_state(config)


def _is_new_thread(thread_id: str) -> bool:
    """True if this thread has no prior messages -- send_message() uses
    this to decide whether to seed the system prompt."""
    return not _snapshot(thread_id).values.get("messages")


def get_state(thread_id: str) -> dict:
    """Read-only snapshot of a thread's current state (messages, reply,
    script, ...), without sending a message. Empty dict for an unknown
    thread_id -- callers checking whether a thread exists should look for
    that rather than catching an exception."""
    return _snapshot(thread_id).values


def send_message(thread_id: str, message: str) -> dict:
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
    nothing useful."""
    config = {"configurable": {"thread_id": thread_id}}
    new_thread = _is_new_thread(thread_id)

    previous = _snapshot(thread_id).values if not new_thread else {}
    already = previous.get("skills") or []
    skills, tools = select(message, already)
    logger.info(
        "send_message: selected skills=[%s] tools=[%s]",
        ", ".join(skills) or "none", ", ".join(tools) or "none",
    )

    update: OrchestrationState = {
        "messages": _seed_messages(
            message, skills, already, new_thread=new_thread, target=previous.get("target"),
        ),
        "attempt": 1,
        "reply": None,
        "script": None,
        "skills": skills,
        "tools": tools,
    }
    return _graph().invoke(update, config=config)


_CODE_BLOCK = re.compile(r"```[a-zA-Z]*\n.*?```", re.S)


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
    notes = [n for n in (_scope_note(message), _target_note(target)) if n]
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


def _scope_note(message: str) -> str | None:
    """Tells the model, from the skill triggers rather than its own
    judgment, that a message names Multistack components. Left to the
    scope rule alone the model refused "what is rke2 cluster" as general
    knowledge, with the rke2 skill already in its prompt.

    Goes inside the user's message rather than as its own system message:
    gpt-oss on vLLM mostly ignored a second system message (billing
    questions answered 1 time in 4), and answered every time with the
    note placed here."""
    mentioned = select_skills(message)
    if not mentioned:
        return None
    return (
        f"[This message names Multistack components ({', '.join(mentioned)}). "
        "Questions about them, including what they are, are in scope.]"
    )


def stream_events(thread_id: str, message: str) -> Iterable[dict]:
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
        {"type": "final",       **<what send_message() returns>}

    "final" is always last. Built on graph.stream(stream_mode="updates"),
    which yields one {node_name: state_delta} per node as it finishes, so
    this is a real trace of what ran: a step that never happened emits
    nothing, and a tool call's args/result here are exactly what
    execute_tools sent to and got back from the real tool.
    """
    config = {"configurable": {"thread_id": thread_id}}
    new_thread = _is_new_thread(thread_id)

    previous = _snapshot(thread_id).values if not new_thread else {}
    already = previous.get("skills") or []
    skills, tools = select(message, already)
    logger.info(
        "stream_events: selected skills=[%s] tools=[%s]",
        ", ".join(skills) or "none", ", ".join(tools) or "none",
    )

    update: OrchestrationState = {
        "messages": _seed_messages(
            message, skills, already, new_thread=new_thread, target=previous.get("target"),
        ),
        "attempt": 1,
        "reply": None,
        "script": None,
        "skills": skills,
        "tools": tools,
    }

    for chunk in _graph().stream(update, config=config, stream_mode="updates"):
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
                    try:
                        result = json.loads(tool_message.content)
                    except (TypeError, ValueError):
                        result = {}
                    yield {
                        "type": "tool_result",
                        "tool": tool_message.name,
                        "valid": result.get("valid") if isinstance(result, dict) else None,
                        "error": result.get("error") if isinstance(result, dict) else None,
                    }

    yield {"type": "final", **_snapshot(thread_id).values}
