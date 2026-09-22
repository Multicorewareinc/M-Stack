"""
Interactive REPL for the agentic pipeline -- an actual back-and-forth
conversation in one session, instead of one static prompt per process.
Tests the parts a single one-shot run can't: answering a clarifying
question in a follow-up message, sending an unrelated second request
after the first one finished. Needs GROQ_API_KEY (or whichever
LLM_PROVIDER is configured) in .env.

    cd agentic/backend/src && python -m examples.chat

Type a request in plain English. Ctrl+C to quit.
"""

import logging

from orchestration.graph import send_message

THREAD_ID = "chat-session"

# orchestration/graph.py's call_model() logs progress at INFO (so a slow
# request, e.g. a large system prompt on a local/CPU model, is visibly
# still working instead of looking identical to a hang), and
# execute_tools() logs each tool call's actual arguments and result at
# DEBUG (so a failure -- or a weaker model not retrying well after one
# -- is diagnosable from what really happened, not just the model's own
# paraphrase of it). Root logger stays at WARNING -- only
# "orchestration" is turned up, so this doesn't also drag in every
# library's own DEBUG-level internals (confirmed noisy in practice:
# mcp's server registered a page of handler-registration lines the
# moment DEBUG applied globally).
logging.basicConfig(level=logging.WARNING, format="[%(name)s] %(message)s")
logging.getLogger("orchestration").setLevel(logging.DEBUG)


def _handle_result(result: dict) -> None:
    """Prints the agent's response for one turn."""
    if result.get("script"):
        print(f"\nagent> {result.get('reply') or 'Here is your script:'}\n")
        print("--- generated script ---")
        print(result["script"])
        print("------------------------\n")
        return

    if result.get("reply"):
        print(f"\nagent> {result['reply']}\n")
        return

    print("\nagent> (no tool call, no reply -- nothing to do with that)\n")


def main():
    print("Agentic pipeline -- interactive session. Ctrl+C to quit.")
    print(f"(thread_id={THREAD_ID!r} -- reused for the whole session, so context carries across turns)\n")

    while True:
        try:
            user_input = input("you> ").strip()
            if not user_input:
                continue
            result = send_message(THREAD_ID, user_input)
        except (KeyboardInterrupt, EOFError):
            print("\nbye.")
            break

        _handle_result(result)


if __name__ == "__main__":
    main()
