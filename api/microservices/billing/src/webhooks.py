"""Stripe webhook verification + processing (ADR-031 AD-06 part 3, add-billing-stripe-webhooks)
— an inbound counterpart to the reporter's/subscriptions' outbound calls, not part of either.

Handles `customer.subscription.updated`, `customer.subscription.deleted`, and
`invoice.payment_failed`, keeping `stripe_customers.subscription_status` in sync with Stripe.
Looks up the row by `stripe_subscription_id` — webhook payloads carry Stripe's own ids, never
billing's internal `principal`/org id.

Transaction shape (design D3): ONE transaction, ONE trailing commit for the whole request.
`session.flush()` (not `commit()`) after adding the idempotency row lets a non-deferred UNIQUE
constraint violation surface synchronously, so a duplicate is detected before any further work
— but the ACTUAL persistence of that idempotency row is deferred to the single trailing commit,
together with any `subscription_status` update. Splitting this into two separate commits would
leave a crash window (a process death between them durably marks an event "seen" while
permanently losing its effect); a single atomic commit has none. A genuine concurrent race (two
deliveries of the same event at once) is handled by Postgres's own row-level locking on the
unique index — no application-level locking needed.
"""

from __future__ import annotations

import json
import logging
from typing import Literal

from models import StripeCustomer, WebhookEvent
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

HANDLED_EVENT_TYPES = {
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.payment_failed",
}

WebhookOutcome = Literal["updated", "ignored", "untracked_subscription", "duplicate"]


def _resolve_subscription_id(event: dict) -> str | None:
    obj = event["data"]["object"]
    if event["type"] == "invoice.payment_failed":
        return obj.get("subscription")
    return obj.get("id")


async def process_webhook(
    payload: bytes, sig_header: str, session: AsyncSession, *, webhook_secret: str,
) -> tuple[int, dict]:
    """Verify and process one webhook request. Returns (http_status, response_body). Never
    raises — a verification failure is a normal 400, not an unhandled exception."""
    import stripe  # lazy: offline tests / the inert path never need this resolvable

    try:
        raw_event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except Exception:
        # partially processed; stripe.SignatureVerificationError is the expected case but a
        # malformed payload can raise other errors from the same call.
        logger.warning("stripe_webhook_verification_failed", exc_info=True)
        return 400, {"error": "invalid signature"}

    # Every helper below treats `event` (and nested objects like event["data"]["object"]) as a
    # plain dict — `.get()`, subscript access, membership. Converting once, immediately, to an
    # actual plain dict (json.dumps(default=str) covers Decimal fields the SDK's own to_dict()
    # still leaves behind) makes that true for the rest of this function, rather than each call
    # site needing to know it's working with a real stripe.Event/Subscription/Invoice object.
    event: dict = json.loads(json.dumps(raw_event.to_dict(), default=str))

    session.add(
        WebhookEvent(stripe_event_id=event["id"], event_type=event["type"], payload=event)
    )
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return 200, {"outcome": "duplicate"}

    outcome: WebhookOutcome
    if event["type"] not in HANDLED_EVENT_TYPES:
        outcome = "ignored"
    else:
        subscription_id = _resolve_subscription_id(event)
        row = (
            await session.execute(
                select(StripeCustomer).where(StripeCustomer.stripe_subscription_id == subscription_id)
            )
        ).scalar_one_or_none()
        if row is None:
            logger.warning(
                "stripe_webhook_untracked_subscription",
                extra={"event_id": event["id"], "stripe_subscription_id": subscription_id},
            )
            outcome = "untracked_subscription"
        else:
            if event["type"] == "invoice.payment_failed":
                row.subscription_status = "past_due"
            else:
                row.subscription_status = event["data"]["object"]["status"]
            outcome = "updated"

    await session.commit()
    return 200, {"outcome": outcome}
