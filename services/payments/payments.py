"""Contributor and dataset-seller payouts for the Robot Deployment Factory.

Flow (Stripe Connect, TEST MODE):
  * Each contributor / dataset seller is a connected account created with
    Accounts v2: Express dashboard, platform-owned fees and losses, and the
    ``recipient`` configuration (``stripe_balance.stripe_transfers``). They finish
    KYC in Stripe-hosted onboarding (``onboarding_link``).
  * The factory (platform) holds a test balance and pays with Transfers once the
    reviewer agent has accepted an episode (or QA has accepted a dataset batch).
  * Every payout is idempotent on the episode / purchase id and is checked
    against the ticket budget before any API call.

SpacetimeDB remains the source of truth: the caller writes the ``payment`` row
(status ``pending``) and reserves the spend in the ticket atomically in a reducer,
then calls these functions, then records the result (``paid`` / ``failed``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import stripe

from .stripe_client import PROVIDER, get_client

CURRENCY = "usd"

# Example prices from CONTEXT.md, in cents.
PRICES_CENTS = {
    "reset": 200,
    "recovery_takeover": 500,
    "clean_teleop": 1000,
}

TRANSFERS_ACTIVE = "active"


# --------------------------------------------------------------------------- errors


class PaymentError(RuntimeError):
    """Base class for payout failures the orchestrator should surface."""


class BudgetExceeded(PaymentError):
    """The payout would push ticket spend over the owner's budget."""


class NotAccepted(PaymentError):
    """Refused: the reviewer / QA agent has not accepted the work."""


class AccountNotReady(PaymentError):
    """The connected account can't receive transfers yet (onboarding incomplete)."""

    def __init__(self, account_id: str, status: str | None):
        super().__init__(f"connected account {account_id} transfers capability is {status!r}, not 'active'")
        self.account_id = account_id
        self.status = status


class InsufficientPlatformBalance(PaymentError):
    """The platform's available test balance is too low to fund the transfer."""


class PlatformNotConfigured(PaymentError):
    """Connect / platform loss-liability setup is missing in the Stripe Dashboard."""


# --------------------------------------------------------------------------- results


@dataclass
class PayoutResult:
    """Maps onto the SpacetimeDB ``payment`` row."""

    contributor_id: str
    episode_id: str  # episode id, or dataset purchase id for dataset payouts
    amount_cents: int
    provider: str = PROVIDER
    status: str = "paid"  # pending | paid | failed | reversed
    provider_ref: str | None = None  # Stripe transfer id (tr_...)
    destination: str | None = None  # connected account id (acct_...)
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


# --------------------------------------------------------------------------- budget guard


def check_budget(budget_cents: int, spent_cents: int, amount_cents: int) -> int:
    """Raise BudgetExceeded unless ``spent + amount <= budget``. Returns remaining after.

    This is a second line of defence: the authoritative check is the SpacetimeDB
    reducer that reserves spend on the ticket atomically before calling Stripe.
    """
    if amount_cents <= 0:
        raise PaymentError(f"amount must be positive, got {amount_cents}")
    if spent_cents + amount_cents > budget_cents:
        raise BudgetExceeded(
            f"payout of {amount_cents}c would exceed budget: spent {spent_cents}c of {budget_cents}c"
        )
    return budget_cents - spent_cents - amount_cents


# --------------------------------------------------------------------------- accounts


def _is_liability_unacknowledged(err: stripe.StripeError) -> bool:
    msg = str(err.user_message or err).lower()
    code = (getattr(err, "code", None) or "").lower()
    return "liability" in code or "losses collector" in msg or "platform as the losses collector" in msg


def create_contributor_account(
    contributor_id: str,
    display_name: str,
    email: str,
    *,
    kind: str = "contributor",  # contributor | dataset_seller
    country: str = "us",
    prefill_identity: dict[str, Any] | None = None,
) -> str:
    """Create (idempotently) a connected account that can receive transfers. Returns acct_ id."""
    client = get_client()
    params: dict[str, Any] = {
        "contact_email": email,
        "display_name": display_name,
        "dashboard": "express",
        "identity": {"country": country, "entity_type": "individual"},
        "configuration": {
            "recipient": {
                "capabilities": {"stripe_balance": {"stripe_transfers": {"requested": True}}}
            }
        },
        "defaults": {
            "currency": CURRENCY,
            "responsibilities": {"fees_collector": "application", "losses_collector": "application"},
        },
        "metadata": {"rdf_contributor_id": contributor_id, "rdf_kind": kind},
        "include": ["configuration.recipient"],
    }
    if prefill_identity:
        params["identity"]["individual"] = prefill_identity
    try:
        account = client.v2.core.accounts.create(
            params, {"idempotency_key": f"rdf-acct-{kind}-{contributor_id}"}
        )
    except stripe.InvalidRequestError as err:
        if _is_liability_unacknowledged(err):
            raise PlatformNotConfigured(
                "Stripe rejected the connected account because the platform has not accepted "
                "loss liability. In the sandbox / test-mode Dashboard open Settings > Connect > Platform "
                "profile (https://dashboard.stripe.com/settings/connect/platform-profile while viewing the sandbox), "
                "complete the loss-liability acknowledgement, then retry."
            ) from err
        raise
    return account.id


def onboarding_link(account_id: str, return_url: str, refresh_url: str) -> str:
    """Single-use Stripe-hosted onboarding URL (expires in minutes; mint a fresh one per visit)."""
    link = get_client().v2.core.account_links.create(
        {
            "account": account_id,
            "use_case": {
                "type": "account_onboarding",
                "account_onboarding": {
                    "configurations": ["recipient"],
                    "return_url": return_url,
                    "refresh_url": refresh_url,
                },
            },
        }
    )
    return link.url


def dashboard_login_link(account_id: str) -> str:
    """Express dashboard login link so a contributor can see their earnings."""
    return get_client().v1.accounts.login_links.create(account_id).url


def transfers_status(account_id: str) -> str | None:
    """v2 capability status for receiving transfers: active | pending | restricted | unsupported."""
    account = get_client().v2.core.accounts.retrieve(account_id, {"include": ["configuration.recipient"]})
    try:
        return account.configuration.recipient.capabilities.stripe_balance.stripe_transfers.status
    except AttributeError:
        return None


# --------------------------------------------------------------------------- balance


def get_balance() -> dict[str, int]:
    """Platform balance in cents (USD): {'available': ..., 'pending': ...}."""
    bal = get_client().v1.balance.retrieve()

    def pick(items) -> int:
        return sum(x.amount for x in items if x.currency == CURRENCY)

    return {"available": pick(bal.available), "pending": pick(bal.pending)}


def top_up_test_balance(amount_cents: int, *, idempotency_key: str | None = None) -> str:
    """TEST MODE: charge the platform with a test card whose funds are available immediately.

    Uses ``pm_card_bypassPending`` so the money lands in the *available* balance
    (normal test charges sit in *pending*). Returns the PaymentIntent id.
    """
    opts = {"idempotency_key": idempotency_key} if idempotency_key else None
    pi = get_client().v1.payment_intents.create(
        {
            "amount": amount_cents,
            "currency": CURRENCY,
            "payment_method": "pm_card_bypassPending",
            "confirm": True,
            "automatic_payment_methods": {"enabled": True, "allow_redirects": "never"},
            "description": "Robot Deployment Factory: test-mode platform top-up",
            "metadata": {"rdf_purpose": "test_topup"},
        },
        opts,
    )
    if pi.status != "succeeded":
        raise PaymentError(f"test top-up {pi.id} ended in status {pi.status}")
    return pi.id


# --------------------------------------------------------------------------- payouts


def _transfer(
    *,
    destination: str,
    amount_cents: int,
    idempotency_key: str,
    ticket_id: str | None,
    description: str,
    metadata: dict[str, str],
) -> stripe.Transfer:
    client = get_client()
    status = transfers_status(destination)
    if status != TRANSFERS_ACTIVE:
        raise AccountNotReady(destination, status)
    params: dict[str, Any] = {
        "amount": amount_cents,
        "currency": CURRENCY,
        "destination": destination,
        "description": description,
        "metadata": metadata,
    }
    if ticket_id:
        params["transfer_group"] = f"ticket_{ticket_id}"
    try:
        return client.v1.transfers.create(params, {"idempotency_key": idempotency_key})
    except stripe.InvalidRequestError as err:
        if "insufficient" in str(err.user_message or err).lower():
            raise InsufficientPlatformBalance(str(err.user_message or err)) from err
        raise


def pay_for_episode(
    contributor_account: str,
    episode_id: str,
    amount_cents: int,
    *,
    contributor_id: str = "",
    ticket_id: str | None = None,
    task_type: str | None = None,
    review_accepted: bool = True,
    budget_cents: int | None = None,
    spent_cents: int = 0,
) -> PayoutResult:
    """Pay a contributor for one reviewer-accepted episode.

    Idempotent on ``episode_id``: calling twice returns the same transfer, never pays twice
    (Stripe idempotency keys last 24h; the SpacetimeDB ``payment`` row is the durable dedupe).
    """
    if not review_accepted:
        raise NotAccepted(f"episode {episode_id} was not accepted by the reviewer; not paying")
    if budget_cents is not None:
        check_budget(budget_cents, spent_cents, amount_cents)
    transfer = _transfer(
        destination=contributor_account,
        amount_cents=amount_cents,
        idempotency_key=f"rdf-episode-{episode_id}",
        ticket_id=ticket_id,
        description=f"Accepted episode {episode_id}" + (f" ({task_type})" if task_type else ""),
        metadata={
            "rdf_kind": "episode",
            "rdf_episode_id": episode_id,
            "rdf_contributor_id": contributor_id,
            "rdf_ticket_id": ticket_id or "",
            "rdf_task_type": task_type or "",
        },
    )
    return _result(transfer, contributor_id or contributor_account, episode_id, amount_cents)


def dataset_billable_episodes(n_requested: int, n_already_uploaded: int, n_accepted: int) -> int:
    """Episodes to pay for in a dataset purchase: N - already uploaded, capped by QA-accepted."""
    return max(0, min(n_requested - n_already_uploaded, n_accepted))


def pay_for_dataset(
    seller_account: str,
    purchase_id: str,
    *,
    n_requested: int,
    n_already_uploaded: int,
    n_accepted: int,
    price_per_episode_cents: int,
    seller_id: str = "",
    ticket_id: str | None = None,
    qa_accepted: bool = True,
    budget_cents: int | None = None,
    spent_cents: int = 0,
) -> PayoutResult:
    """Pay a dataset seller for QA-accepted episodes we don't already have.

    One transfer per purchase batch, idempotent on ``purchase_id``. For partial deliveries,
    use a new purchase_id per delivered batch (e.g. ``<purchase>-batch2``).
    """
    if not qa_accepted:
        raise NotAccepted(f"dataset purchase {purchase_id} not accepted by QA; not paying")
    billable = dataset_billable_episodes(n_requested, n_already_uploaded, n_accepted)
    if billable == 0:
        raise PaymentError(f"dataset purchase {purchase_id} has no billable episodes")
    amount = billable * price_per_episode_cents
    if budget_cents is not None:
        check_budget(budget_cents, spent_cents, amount)
    transfer = _transfer(
        destination=seller_account,
        amount_cents=amount,
        idempotency_key=f"rdf-dataset-{purchase_id}",
        ticket_id=ticket_id,
        description=f"Dataset purchase {purchase_id}: {billable} accepted episodes",
        metadata={
            "rdf_kind": "dataset",
            "rdf_purchase_id": purchase_id,
            "rdf_seller_id": seller_id,
            "rdf_ticket_id": ticket_id or "",
            "rdf_billable_episodes": str(billable),
            "rdf_price_per_episode_cents": str(price_per_episode_cents),
        },
    )
    return _result(transfer, seller_id or seller_account, purchase_id, amount)


def reverse_payment(transfer_id: str, *, reason: str = "") -> str:
    """Claw back a payout (e.g. an episode later found bad). Returns the reversal id."""
    rev = get_client().v1.transfers.reversals.create(
        transfer_id, {"metadata": {"rdf_reason": reason[:400]}}
    )
    return rev.id


def _result(transfer: stripe.Transfer, payee_id: str, ref_id: str, amount_cents: int) -> PayoutResult:
    return PayoutResult(
        contributor_id=payee_id,
        episode_id=ref_id,
        amount_cents=amount_cents,
        status="reversed" if transfer.reversed else "paid",
        provider_ref=transfer.id,
        destination=transfer.destination if isinstance(transfer.destination, str) else transfer.destination.id,
        raw={"transfer_group": transfer.transfer_group, "created": transfer.created},
    )


# --------------------------------------------------------------------------- budget funding (optional)


def create_budget_checkout(
    ticket_id: str, amount_cents: int, *, success_url: str, cancel_url: str
) -> tuple[str, str]:
    """Checkout Session the ticket owner uses to fund a ticket's budget (test cards only).

    Returns (session_id, url). Credit the budget from the ``checkout.session.completed``
    webhook (see webhooks.py), not from the success page.
    """
    import secrets
    import string

    suffix = "".join(secrets.choice(string.ascii_lowercase) for _ in range(8))
    session = get_client().v1.checkout.sessions.create(
        {
            "mode": "payment",
            "line_items": [
                {
                    "quantity": 1,
                    "price_data": {
                        "currency": CURRENCY,
                        "unit_amount": amount_cents,
                        "product_data": {"name": f"Robot Deployment Factory budget, ticket {ticket_id} (TEST MODE)"},
                    },
                }
            ],
            "success_url": success_url,
            "cancel_url": cancel_url,
            "client_reference_id": ticket_id,
            "metadata": {"rdf_purpose": "ticket_budget", "rdf_ticket_id": ticket_id},
            "payment_intent_data": {"transfer_group": f"ticket_{ticket_id}"},
            "integration_identifier": f"rdf-budget-{suffix}",
        }
    )
    return session.id, session.url
