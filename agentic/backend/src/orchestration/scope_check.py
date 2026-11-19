"""Catches a reply that actually answers something outside Multistack
deployment planning, as a deterministic backstop behind the SCOPE
instruction in knowledge.py -- not a replacement for it.

Prompt injection testing (a crafted retrieved passage telling the model
the SCOPE restriction is outdated) showed the instruction alone isn't
reliable: retrieved content read as more insistent than the system
prompt got the model to answer an unrelated question anyway. The
instruction can't be made to hold by wording it more strongly -- any
text in context can in principle out-argue any other text in context.
This checks the actual output instead of trusting the instruction was
followed, the same reason _mask_secrets (graph.py) runs on every reply
unconditionally rather than trusting "never repeat a password" to hold.

Deliberately coarse: it asks one yes/no question and, on yes, discards
the whole reply for the standard decline -- it does not try to surgically
keep an on-topic part a mixed reply might have, which needs the kind of
judgement this backstop exists because that judgement can't be trusted
under adversarial input. A reply that correctly declines something in
one sentence (the normal, instructed behaviour) is not a violation and
answers "no" here; one that actually engages with the off-topic part --
answers it, writes it, speculates on it -- answers "yes"."""

from __future__ import annotations

import logging
from collections.abc import Callable

from langchain_core.messages import HumanMessage, SystemMessage

from orchestration.knowledge import RESOURCES

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "You check one thing: does the reply below actually ANSWER, WRITE, or ENGAGE WITH "
    "something that has nothing to do with planning a Multistack deployment (SDK components, "
    "scripts, fields, clusters) -- a poem, a prediction, general advice, code unrelated to "
    "Multistack, anything else? A Multistack deployment is made of these, and a reply about any "
    "of them, their settings, the script that deploys them or the values needed to deploy them "
    "is on topic: " + RESOURCES + ". Declining something in one sentence is not engaging with "
    "it. Reply with exactly one word: yes (it engages with off-topic content) or no (it doesn't)."
)


def violates(reply: str, ask: Callable[[list], str]) -> bool:
    """True if `reply` actually answers something outside Multistack
    deployment planning, per one judgement call. Fails closed to False
    (never worse off than without this check) on any error -- a missed
    violation is this module's whole risk surface, but a check that can
    crash the reply path is worse than one that occasionally misses."""
    if not reply.strip():
        return False
    try:
        answer = ask([SystemMessage(content=INSTRUCTIONS), HumanMessage(content=reply)])
    except Exception:
        return False
    return (answer or "").strip().lower().startswith("yes")


REPLACEMENT = (
    "Sorry, that's outside what I can help with -- I only plan Multistack deployments. "
    "(An earlier draft of this reply strayed outside that scope and was replaced.)"
)


def enforce(reply: str, ask: Callable[[list], str], *, fallback: str | None = None) -> str:
    """`reply`, or what to say instead if it violates scope: `fallback`
    when the caller has text of its own that is known to be on topic (a
    validated script's summary, a question written from the tool's own
    findings), REPLACEMENT otherwise."""
    if not violates(reply, ask):
        return reply
    # Counted from the log: how often a reply is replaced is this check's
    # false-alarm rate on real requests. The reply itself is not logged.
    logger.info(
        "scope: a reply of %d characters was replaced with %s",
        len(reply), "the caller's own text" if fallback is not None else "the standard decline",
    )
    return fallback if fallback is not None else REPLACEMENT
