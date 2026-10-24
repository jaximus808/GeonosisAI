"""Stripe webhook handling (signature-verified).

Needed for:
  * ``checkout.session.completed`` / ``checkout.session.async_payment_succeeded``:
    credit a ticket's budget after the owner funds it through Checkout.
  * Connected-account capability changes (thin v2 events such as
    ``v2.core.account[configuration.recipient].capability_status_updated``):
    flip a contributor to "can be paid" in SpacetimeDB.
  * ``transfer.reversed``: mark a payment row ``reversed``.

Local dev: ``stripe listen --forward-to localhost:<port>/stripe/webhook`` and put the
printed ``whsec_...`` in ``STRIPE_WEBHOOK_SECRET`` (never commit or print it).
Thin v2 events need a separate event destination (``stripe listen --forward-thin-to``).
"""

from __future__ import annotations

import os
from typing import Any, Callable

import stripe

from .stripe_client import get_client

Handler = Callable[[Any], None]


def _secret() -> str:
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    if not secret:
        raise RuntimeError("STRIPE_WEBHOOK_SECRET is not set")
    return secret


def parse_event(payload: bytes, sig_header: str, secret: str | None = None) -> stripe.Event:
    """Verify the signature and return the event. Raises stripe.SignatureVerificationError."""
    return get_client().construct_event(payload, sig_header, secret or _secret())


def handle_event(event: stripe.Event, handlers: dict[str, Handler]) -> bool:
    """Dispatch a verified snapshot event. Returns True if a handler ran.

    Checkout fulfilment must ignore ``checkout.session.completed`` while
    ``payment_status == 'unpaid'`` (async methods confirm later).
    """
    if event.type in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        if getattr(event.data.object, "payment_status", None) == "unpaid":
            return False
    fn = handlers.get(event.type)
    if fn is None:
        return False
    fn(event.data.object)
    return True
