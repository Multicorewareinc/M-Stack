"""
Interactive REPL for the agentic pipeline -- an actual back-and-forth
conversation in one session, instead of one static prompt per process.
Tests the parts a single one-shot run can't: answering a clarifying
question in a follow-up message, sending an unrelated second request
after the first one finished. Needs GROQ_API_KEY (or whichever
LLM_PROVIDER is configured) in .env.

    cd agentic/backend/src && python -m examples.chat

Type a request in plain English. Ctrl+C to quit.

Quiet by default (just "you>"/"agent>"). Set AGENTIC_CHAT_VERBOSE=1 to
bring back orchestration/graph.py's own progress+tool-call logging --
useful when a failure, or a weaker model not retrying well after one,
needs diagnosing from what actually happened rather than the model's own
paraphrase of it.
"""

import asyncio
import logging
import os

from orchestration.graph import send_message

THREAD_ID = "chat-session"

# Quiet by default: root at WARNING, and httpx/httpcore's own request
# logging pinned to WARNING too (confirmed in practice: httpx logs each
# request at INFO regardless of root's level, so root alone doesn't
# silence it). AGENTIC_CHAT_VERBOSE=1 restores "orchestration"'s DEBUG
# logging -- see the module docstring for why that's worth having on
# demand, just not as the default for a plain interactive session.
#
# force=True: something already installs a root handler (a rich-style
# one, going by the "graph.py:373"-tagged output seen without this --
# some transitive langgraph/langchain import) by the time this runs,
# which makes a plain basicConfig() silently do nothing (its documented
# behaviour once the root logger already has a handler). force=True
# replaces it instead of losing this call to whichever import won the
# race.
logging.basicConfig(level=logging.WARNING, format="[%(name)s] %(message)s", force=True)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
if os.environ.get("AGENTIC_CHAT_VERBOSE", "").lower() in ("1", "true", "yes"):
    logging.getLogger("orchestration").setLevel(logging.DEBUG)


def _handle_result(result: dict) -> list[str]:
    """Prints the agent's response for one turn. Returns the options it
    offered when the turn paused on a choice, else an empty list."""
    ask = result.get("ask_user")
    if ask:
        options = ask.get("options") or []
        print(f"\nagent> {ask.get('question')}")
        for n, option in enumerate(options, start=1):
            print(f"         {n}. {option}")
        print("       (type a number, or your own answer)\n" if options else "")
        return options

    if result.get("script"):
        print(f"\nagent> {result.get('reply') or 'Here is your script:'}\n")
        print("--- generated script ---")
        print(result["script"])
        print("------------------------\n")
        return []

    if result.get("reply"):
        print(f"\nagent> {result['reply']}\n")
        return []

    print("\nagent> (no tool call, no reply -- nothing to do with that)\n")
    return []


async def main():
    print("Agentic pipeline -- interactive session. Ctrl+C to quit.")
    print(f"(thread_id={THREAD_ID!r} -- reused for the whole session, so context carries across turns)\n")

    options: list[str] = []
    while True:
        try:
            # input() is blocking, but that's fine here: this is a
            # single-user REPL with nothing else running on the loop --
            # send_message is the only part that needs to be a real
            # await (see orchestration/graph.py's call_model, which now
            # awaits the model instead of blocking a thread on it).
            user_input = input("you> ").strip()
            if not user_input:
                continue
            # A number picks one of the options the last turn offered.
            if user_input.isdigit() and 1 <= int(user_input) <= len(options):
                user_input = options[int(user_input) - 1]
            result = await send_message(THREAD_ID, user_input)
        except (KeyboardInterrupt, EOFError):
            print("\nbye.")
            break

        options = _handle_result(result)


if __name__ == "__main__":
    asyncio.run(main())
