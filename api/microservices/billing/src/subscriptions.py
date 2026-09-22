"""Stripe Subscription creation/update (ADR-031 AD-06 part 2, add-billing-plan-subscription-
linkage) — a synchronous HTTP-triggered counterpart to the reporter's per-event drain loop, not
part of it. Reuses `reporter.py::create_or_get_stripe_customer` as a library call (a brand-new
org may have no `stripe_customers` row yet, since this endpoint can fire before any usage event
has been metered for it) rather than duplicating customer-creation logic.
"""

from __future__ import annotations

import logging
from typing import Literal

from models import StripeCustomer
from reporter import create_or_get_stripe_customer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

SubscriptionResult = Literal["created", "updated", "noop", "transient_failure", "permanent_failure"]


async def create_or_update_stripe_subscription(
    client, session: AsyncSession, *, principal: str, stripe_price_id: str, stripe_customer_id: str,
) -> tuple[SubscriptionResult, str | None]:
    """Create a Stripe Subscription for the org's Customer × price when none exists yet; update
    the existing one in place when the requested price differs from the stored one; no-op when
    it already matches. Never raises — same top-level exception classification as
    `report_to_stripe`/`create_or_get_stripe_customer` (copied, not re-derived).

    `payment_behavior="default_incomplete"` (self-serve-card model): the Customer has no
    payment method on it yet (no checkout UI collects one today), so this lets the Subscription
    get created anyway, sitting `incomplete` with a PaymentIntent attached until a future
    checkout step completes it — rather than Stripe rejecting the create outright, which is
    what the plain default does when the Customer has no payment source."""
    row = (
        await session.execute(select(StripeCustomer).where(StripeCustomer.principal == principal))
    ).scalar_one()

    import stripe  # lazy: offline tests / the inert path never need this resolvable

    if row.stripe_subscription_id is None:
        # The idempotency key carries an attempt count, not just the principal: Stripe replays
        # a create's exact response for any retry sharing its key, so a retry after a failed
        # attempt needs a fresh key to actually be retried rather than replay the old result.
        row.subscription_create_attempts += 1
        try:
            subscription = client.subscriptions.create(
                {
                    "customer": stripe_customer_id,
                    "items": [{"price": stripe_price_id}],
                    "payment_behavior": "default_incomplete",
                },
                options={
                    "idempotency_key": (
                        f"billing:subscription:create:{principal}:{row.subscription_create_attempts}"
                    )
                },
            )
        except stripe.APIConnectionError:
            logger.warning("stripe_subscription_create_transient_connection", exc_info=True)
            return "transient_failure", None
        except stripe.RateLimitError:  # MUST precede InvalidRequestError (subclass ordering)
            logger.warning("stripe_subscription_create_transient_rate_limited", exc_info=True)
            return "transient_failure", None
        except stripe.APIError:
            logger.warning("stripe_subscription_create_transient_api_error", exc_info=True)
            return "transient_failure", None
        except stripe.InvalidRequestError:
            logger.warning("stripe_subscription_create_permanent_invalid_request", exc_info=True)
            return "permanent_failure", None
        except stripe.StripeError:  # catch-all (e.g. AuthenticationError) — retry cannot help
            logger.warning("stripe_subscription_create_permanent_other", exc_info=True)
            return "permanent_failure", None

        row.stripe_subscription_id = subscription.id
        row.stripe_price_id = stripe_price_id
        return "created", subscription.id

    if row.stripe_price_id != stripe_price_id:
        try:
            client.subscriptions.modify(
                row.stripe_subscription_id,
                {"items": [{"price": stripe_price_id}]},
                options={
                    "idempotency_key": f"billing:subscription:update:{principal}:{stripe_price_id}"
                },
            )
        except stripe.APIConnectionError:
            logger.warning("stripe_subscription_update_transient_connection", exc_info=True)
            return "transient_failure", None
        except stripe.RateLimitError:  # MUST precede InvalidRequestError (subclass ordering)
            logger.warning("stripe_subscription_update_transient_rate_limited", exc_info=True)
            return "transient_failure", None
        except stripe.APIError:
            logger.warning("stripe_subscription_update_transient_api_error", exc_info=True)
            return "transient_failure", None
        except stripe.InvalidRequestError:
            logger.warning("stripe_subscription_update_permanent_invalid_request", exc_info=True)
            return "permanent_failure", None
        except stripe.StripeError:  # catch-all (e.g. AuthenticationError) — retry cannot help
            logger.warning("stripe_subscription_update_permanent_other", exc_info=True)
            return "permanent_failure", None

        row.stripe_price_id = stripe_price_id
        return "updated", row.stripe_subscription_id

    return "noop", row.stripe_subscription_id


async def resolve_and_link_subscription(
    client, session: AsyncSession, *, principal: str, stripe_price_id: str | None,
) -> dict:
    """Endpoint orchestration: a falsy `stripe_price_id` is an inert no-op (no customer
    resolution, no Stripe call, no DB write) — checked BEFORE anything else. Otherwise resolves
    the org's Stripe Customer first (reusing Phase 1's function; never bypassed), then
    creates/updates/no-ops the Subscription. Does not commit — the caller (get_session) owns
    the transaction."""
    if not stripe_price_id:
        return {"outcome": "inert", "stripe_subscription_id": None}

    customer_outcome, stripe_customer_id = await create_or_get_stripe_customer(
        client, session, principal=principal
    )
    if customer_outcome in ("transient_failure", "permanent_failure"):
        return {"outcome": customer_outcome, "stripe_subscription_id": None}

    outcome, stripe_subscription_id = await create_or_update_stripe_subscription(
        client, session, principal=principal, stripe_price_id=stripe_price_id,
        stripe_customer_id=stripe_customer_id,
    )
    return {"outcome": outcome, "stripe_subscription_id": stripe_subscription_id}
