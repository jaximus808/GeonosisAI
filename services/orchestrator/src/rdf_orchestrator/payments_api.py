"""TDM-63: payments HTTP surface (Stripe TEST MODE). Mounted by ``app.create_app``.

  POST /stripe/webhook                               signature-verified Stripe events:
        transfer.created   -> mark_payment_paid for a still-pending/awaiting row (async confirmation;
                              the handler normally reports the transfer synchronously first)
        transfer.reversed  -> mark_payment_reversed
        account.updated    -> re-check the connected account and set_contributor_payout_account
  POST /payments/contributors/{id}/onboarding        create the contributor's connected account if
        needed and return a fresh Stripe-hosted onboarding link (for the earnings page)
  POST /payments/contributors/{id}/refresh           re-check stripe_transfers -> payouts_enabled
  GET  /payments/mode                                {"mode": "test", "badge": ...} for the UI badge

Local webhooks: ``stripe listen --forward-to localhost:8000/stripe/webhook`` and put the printed
whsec_ in .env as STRIPE_WEBHOOK_SECRET (never print it). Without it the webhook answers 503.
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .handlers import payments as ph
from .log import get_logger

log = get_logger("payments_api")
router = APIRouter()

TEST_BADGE = "Stripe TEST MODE: no real money"


def _bus(request: Request) -> Any:
    return getattr(request.app.state, "bus", None)


def _payments(request: Request) -> list:
    bus = _bus(request)
    return bus.rows("payment") if bus is not None else []


def _contributor(request: Request, cid: int) -> Any:
    bus = _bus(request)
    c = bus.get("contributor", cid) if bus is not None else None
    if c is None:
        raise HTTPException(404, f"contributor {cid} not found")
    return c


def _set_account(cid: int, acct: str, enabled: bool) -> None:
    ph.executor.reducers.call("set_contributor_payout_account", cid, acct, enabled)


@router.get("/payments/mode")
def mode() -> dict:
    return {"mode": "test", "provider": ph.PROVIDER, "badge": TEST_BADGE}


@router.post("/stripe/webhook")
async def stripe_webhook(request: Request) -> dict:
    if not os.environ.get("STRIPE_WEBHOOK_SECRET"):
        raise HTTPException(503, "STRIPE_WEBHOOK_SECRET is not set (run `stripe listen` and add it to .env)")
    import stripe
    from rdf_payments.webhooks import parse_event

    payload = await request.body()
    try:
        event = parse_event(payload, request.headers.get("stripe-signature", ""))
    except (stripe.SignatureVerificationError, ValueError) as e:
        raise HTTPException(400, f"bad signature or payload: {type(e).__name__}") from None
    obj = event.data.object.to_dict()  # StripeObject is not a dict in stripe>=13
    handled = apply_event(event.type, obj, _payments(request), _bus(request))
    log.info("stripe.webhook", extra={"fields": {"type": event.type, "id": event.id, "handled": handled}})
    return {"received": True, "handled": handled}


def apply_event(etype: str, obj: Any, payments: list, bus: Any = None) -> str | None:
    """Map one verified Stripe event onto reducer calls. Returns what it did (for logs and tests)."""
    if etype == "transfer.created":
        ref = (obj.get("metadata") or {}).get("rdf_ref")
        for p in payments:
            if p.get("status") in ph.PAYABLE_STATUSES and ref and ph.payout_ref(p) == ref:
                ph.executor.reducers.call("mark_payment_paid", int(p["id"]), ph.PROVIDER, obj["id"])
                return f"paid:{p['id']}"
        return None
    if etype == "transfer.reversed":
        for p in payments:
            if p.get("provider_ref") == obj.get("id") and p.get("status") == "paid":
                ph.executor.reducers.call("mark_payment_reversed", int(p["id"]), "stripe transfer.reversed")
                return f"reversed:{p['id']}"
        return None
    if etype == "account.updated" and bus is not None:
        acct = obj.get("id")
        for c in bus.rows("contributor"):
            if c.get("stripe_account_id") == acct:
                from rdf_payments import transfers_status

                enabled = transfers_status(acct) == "active"
                _set_account(int(c["id"]), acct, enabled)
                return f"contributor:{c['id']}:{enabled}"
        return None
    return None


class OnboardingRequest(BaseModel):
    return_url: str
    refresh_url: str
    email: str | None = None


@router.post("/payments/contributors/{cid}/onboarding")
def onboarding(cid: int, body: OnboardingRequest, request: Request) -> dict:
    from rdf_payments import PlatformNotConfigured, create_contributor_account, onboarding_link

    c = _contributor(request, cid)
    acct = c.get("stripe_account_id")
    if not acct:
        try:
            acct = create_contributor_account(
                str(cid), c.get("name") or f"contributor {cid}",
                body.email or f"contributor-{cid}@example.com",
                kind=c.get("kind") or "contributor",
            )
        except PlatformNotConfigured as e:
            # morning question #8: the user must accept platform loss liability in the Dashboard
            raise HTTPException(409, f"Stripe Connect is not set up for this platform yet: {e}") from None
        _set_account(cid, acct, False)
    return {"stripe_account_id": acct, "onboarding_url": onboarding_link(acct, body.return_url, body.refresh_url),
            "mode": "test", "badge": TEST_BADGE}


@router.post("/payments/contributors/{cid}/refresh")
def refresh(cid: int, request: Request) -> dict:
    from rdf_payments import transfers_status

    c = _contributor(request, cid)
    acct = c.get("stripe_account_id")
    if not acct:
        return {"stripe_account_id": None, "payouts_enabled": False, "transfers_status": None}
    status = transfers_status(acct)
    enabled = status == "active"
    if enabled != bool(c.get("payouts_enabled")):
        _set_account(cid, acct, enabled)
    return {"stripe_account_id": acct, "payouts_enabled": enabled, "transfers_status": status}
