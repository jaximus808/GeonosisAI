"""TDM-63: Stripe TEST-MODE payouts for reserved ``payment`` rows.

SpacetimeDB decides *whether* to pay: ``submit_review`` (accept) / ``reserve_payment`` /
``reserve_task_payment`` / ``reserve_dataset_payment`` insert a ``payment`` row with
``status='pending'`` after the review and budget checks (TDM-11). Rejected episodes never get a
row, so nothing here can pay for them. This handler only *moves the money*, once:

    payment enters 'pending'
      -> contributor has no stripe_account_id / payouts_enabled=false
             -> mark_payment_awaiting_onboarding   (budget stays reserved; NOT failed)
      -> Stripe Transfer from the platform test balance (rdf_payments.pay_reserved)
             key rdf-episode-<episode_id> | rdf-task-<task_id> | rdf-dataset-<offer>-<batch_ref>
             -> mark_payment_paid(payment_id, 'stripe_test', 'tr_...')
             -> account not transfer-capable  -> mark_payment_awaiting_onboarding
             -> platform test balance too low -> one test top-up (pm_card_bypassPending), retry
             -> Stripe refused (permanent)    -> mark_payment_failed(reason)   (releases budget)
             -> network / 5xx / rate limit    -> retried in-handler with backoff, then failed

    contributor becomes payable (stripe_account_id set and payouts_enabled)
      -> every 'awaiting_onboarding' row of theirs goes through the same transfer path

Idempotent by construction (the bus re-fires on boot snapshots and resyncs):
  * the current row is re-read from the bus mirror and skipped unless pending/awaiting_onboarding;
  * one in-process claim per payment id, so concurrent fires for the same row collapse;
  * the Stripe idempotency key returns the same transfer for 24h, and ``find_transfer`` (metadata
    ``rdf_ref``) finds it after that, so a retry never pays twice;
  * ``mark_payment_paid`` is a no-op for the same provider_ref.
If the transfer succeeds but reporting it fails, the row stays pending and the next fire finds
the existing transfer and reports it.

Stripe stays in TEST MODE: ``rdf_payments.stripe_client`` refuses anything but sk_test_/rk_test_.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from ..bus import Event, on_enter, watch
from ..log import get_logger

log = get_logger("payments")

PROVIDER = "stripe_test"
PAYABLE_STATUSES = ("pending", "awaiting_onboarding")
TRANSIENT_BACKOFF_S = (1.0, 2.0, 4.0)
TOPUP_BUFFER_CENTS = 2000


# --------------------------------------------------------------------------- seams (faked in tests)


class StripeOps(Protocol):
    """What the payout path needs from Stripe (``RealStripeOps`` or a test fake)."""

    def pay(self, destination: str, ref: str, amount_cents: int, *, payee_id: str, ticket_id: str | None,
            description: str, metadata: dict[str, str]) -> str: ...  # returns the tr_ id
    def top_up(self, amount_cents: int) -> str: ...

    # error classification (so the executor never imports stripe itself)
    def classify(self, err: BaseException) -> str: ...  # not_ready | low_balance | transient | permanent


class Reducers(Protocol):
    def call(self, reducer: str, *args: Any) -> None: ...


class RealStripeOps:
    """rdf_payments (Stripe Connect transfers, TEST MODE). Imported lazily so the orchestrator boots
    without a Stripe key; the first payout then fails loudly and the row stays pending."""

    def __init__(self) -> None:
        import rdf_payments  # noqa: F401 - fail fast if the package is missing

        from rdf_payments.stripe_client import get_client

        get_client()  # raises LiveModeRefused for a missing / non-test key

    def pay(self, destination, ref, amount_cents, *, payee_id, ticket_id, description, metadata) -> str:
        from rdf_payments import pay_reserved

        res = pay_reserved(destination, ref, amount_cents, payee_id=payee_id, ticket_id=ticket_id,
                           description=description, metadata=metadata)
        if res.status != "paid" or not res.provider_ref:
            from rdf_payments import PaymentError

            raise PaymentError(f"transfer {res.provider_ref} for {ref} is {res.status}")
        return res.provider_ref

    def top_up(self, amount_cents: int) -> str:
        from rdf_payments import top_up_test_balance

        return top_up_test_balance(amount_cents)

    def classify(self, err: BaseException) -> str:
        import stripe
        from rdf_payments import AccountNotReady, InsufficientPlatformBalance

        if isinstance(err, AccountNotReady):
            return "not_ready"
        if isinstance(err, InsufficientPlatformBalance):
            return "low_balance"
        if isinstance(err, (stripe.APIConnectionError, stripe.RateLimitError)):
            return "transient"
        if isinstance(err, stripe.APIError) and (getattr(err, "http_status", None) or 500) >= 500:
            return "transient"
        return "permanent"


class HttpReducers:
    """Reducer calls over the shared keep-alive HTTP client: raises SpacetimeError on a refusal, which
    the socket-based ``bus.call_reducer`` (fire-and-forget) would not tell us."""

    def __init__(self) -> None:
        from rdf_common.spacetime_client import SpacetimeClient, load_dotenv

        load_dotenv()
        self._c = SpacetimeClient()

    def call(self, reducer: str, *args: Any) -> None:
        self._c.call_reducer(reducer, *args)


# --------------------------------------------------------------------------- the payout path


def payout_ref(p: Any) -> str:
    """Stripe idempotency key (and ``metadata.rdf_ref``) for a payment row."""
    if int(p.get("episode_id") or 0) > 0:
        return f"rdf-episode-{p['episode_id']}"
    batch = p.get("batch_ref") or ""
    if int(p.get("dataset_offer_id") or 0) > 0:
        return f"rdf-dataset-{p['dataset_offer_id']}-{batch}"
    if batch.startswith("task:"):
        return f"rdf-task-{batch.removeprefix('task:')}"
    return f"rdf-payment-{p['id']}"


def _describe(p: Any) -> str:
    if int(p.get("episode_id") or 0) > 0:
        what = f"accepted episode {p['episode_id']}"
    elif int(p.get("dataset_offer_id") or 0) > 0:
        what = f"dataset offer {p['dataset_offer_id']} batch {p.get('batch_ref')}"
    else:
        what = f"task {(p.get('batch_ref') or '').removeprefix('task:') or p['id']}"
    return f"Robot Deployment Factory (TEST MODE): {what}, ticket {p.get('ticket_id')}"


@dataclass
class Outcome:
    payment_id: int
    result: str  # paid | awaiting_onboarding | failed | skipped | error
    detail: str = ""
    provider_ref: str | None = None


class PayoutExecutor:
    """Takes factories, not instances, so importing the handler (and booting the app) needs no Stripe
    key or HTTP client; both are built on the first payout."""

    def __init__(self, stripe_factory: Callable[[], StripeOps], reducers_factory: Callable[[], Reducers],
                 *, sleep: Callable[[float], None] = time.sleep, backoff: tuple[float, ...] = TRANSIENT_BACKOFF_S):
        self._stripe_factory, self._reducers_factory = stripe_factory, reducers_factory
        self._stripe: StripeOps | None = None
        self._reducers: Reducers | None = None
        self._sleep, self._backoff = sleep, backoff
        self._inflight: set[int] = set()
        self._lock = threading.Lock()

    @property
    def stripe(self) -> StripeOps:
        if self._stripe is None:
            self._stripe = self._stripe_factory()
        return self._stripe

    @property
    def reducers(self) -> Reducers:
        if self._reducers is None:
            self._reducers = self._reducers_factory()
        return self._reducers

    def _report(self, reducer: str, *args: Any, attempts: int = 3) -> None:
        for i in range(attempts):
            try:
                self.reducers.call(reducer, *args)
                return
            except Exception as e:  # noqa: BLE001
                status = getattr(e, "status", None)
                if (isinstance(status, int) and 400 <= status < 500) or i == attempts - 1:
                    raise  # a refusal (e.g. row moved on) or out of retries: leave it to the next fire
                self._sleep(self._backoff[min(i, len(self._backoff) - 1)] if self._backoff else 0)

    def handle(self, payment: Any, contributor: Any | None, episode: Any | None = None) -> Outcome:
        pid = int(payment["id"])
        if payment.get("status") not in PAYABLE_STATUSES:
            return Outcome(pid, "skipped", f"status {payment.get('status')}")
        with self._lock:
            if pid in self._inflight:
                return Outcome(pid, "skipped", "already in flight")
            self._inflight.add(pid)
        try:
            return self._handle(payment, contributor, episode)
        finally:
            with self._lock:
                self._inflight.discard(pid)

    def _awaiting(self, p: Any, note: str) -> Outcome:
        pid = int(p["id"])
        if p.get("status") == "pending":
            self._report("mark_payment_awaiting_onboarding", pid, note)
        return Outcome(pid, "awaiting_onboarding", note)

    def _fail(self, p: Any, reason: str) -> Outcome:
        pid = int(p["id"])
        self._report("mark_payment_failed", pid, reason[:500])
        return Outcome(pid, "failed", reason)

    def _handle(self, p: Any, c: Any | None, ep: Any | None) -> Outcome:
        pid, amount = int(p["id"]), int(p["amount_cents"])
        if ep is not None and ep.get("review_status") == "rejected":
            # Defence in depth: the reducers never reserve a payout for a rejected episode. (Only an
            # explicit 'rejected' blocks: the accept and the payment row land in one transaction, and
            # the mirror may apply the payment insert a moment before the episode update.)
            return self._fail(p, f"episode {ep.get('id')} was rejected by the reviewer; not paying")
        if c is None:
            return self._fail(p, f"contributor {p.get('contributor_id')} not found")
        acct = c.get("stripe_account_id")
        if not acct or not c.get("payouts_enabled"):
            why = "no Stripe connected account yet" if not acct else "Stripe payouts not enabled yet"
            return self._awaiting(p, f"{why}: contributor {c['id']} must finish Stripe onboarding (TEST MODE)")

        ref = payout_ref(p)
        ticket = str(p.get("ticket_id")) if p.get("ticket_id") else None
        meta = {
            "rdf_payment_id": str(pid),
            "rdf_contributor_id": str(c["id"]),
            "rdf_ticket_id": ticket or "",
            "rdf_episode_id": str(p.get("episode_id") or 0),
            "rdf_dataset_offer_id": str(p.get("dataset_offer_id") or 0),
            "rdf_batch_ref": p.get("batch_ref") or "",
        }
        topped_up, transient_tries = False, 0
        while True:
            try:
                tr = self.stripe.pay(acct, ref, amount, payee_id=str(c["id"]), ticket_id=ticket,
                                     description=_describe(p), metadata=meta)
                break
            except Exception as e:  # noqa: BLE001 - classified below
                kind = self.stripe.classify(e)
                msg = f"{type(e).__name__}: {getattr(e, 'user_message', None) or e}"
                if kind == "not_ready":
                    return self._awaiting(p, f"Stripe account {acct} cannot receive transfers yet ({msg})")
                if kind == "low_balance" and not topped_up:
                    topped_up = True
                    pi = self.stripe.top_up(amount + TOPUP_BUFFER_CENTS)
                    log.warning("payments.test_topup", extra={"fields": {"payment_id": pid, "payment_intent": pi}})
                    continue
                if kind == "transient" and transient_tries < len(self._backoff):
                    self._sleep(self._backoff[transient_tries])
                    transient_tries += 1
                    continue
                return self._fail(p, f"stripe transfer failed ({kind}): {msg}")

        self._report("mark_payment_paid", pid, PROVIDER, tr)
        log.info("payments.paid", extra={"fields": {
            "payment_id": pid, "provider_ref": tr, "amount_cents": amount, "destination": acct, "ref": ref}})
        return Outcome(pid, "paid", ref, tr)


# --------------------------------------------------------------------------- bus wiring

executor = PayoutExecutor(RealStripeOps, HttpReducers)
watch("episode")  # review_status guard (read from the mirror, never written here)


def _contributor(bus: Any, contributor_id: Any) -> Any | None:
    return bus.get("contributor", contributor_id) if bus is not None else None


def _episode(bus: Any, p: Any) -> Any | None:
    eid = int(p.get("episode_id") or 0)
    return bus.get("episode", eid) if bus is not None and eid > 0 else None


def _log_outcome(o: Outcome) -> None:
    lvl = log.warning if o.result in ("failed", "awaiting_onboarding") else log.info
    lvl("payments.outcome", extra={"fields": {"payment_id": o.payment_id, "result": o.result,
                                               "detail": o.detail, "provider_ref": o.provider_ref}})


@on_enter("payment", where=lambda r: r.status == "pending", name="payments.payout_pending")
def payout_pending(ev: Event) -> None:
    bus = ev.bus
    row = (bus.get("payment", ev.row["id"]) if bus is not None else None) or ev.row  # newest state
    _log_outcome(executor.handle(row, _contributor(bus, row.get("contributor_id")), _episode(bus, row)))


def _payable(r: Any) -> bool:
    return bool(r.get("stripe_account_id")) and bool(r.get("payouts_enabled"))


@on_enter("contributor", where=_payable, name="payments.contributor_payable")
def contributor_payable(ev: Event) -> None:
    """A contributor finished onboarding: pay whatever was waiting for them."""
    bus = ev.bus
    if bus is None:
        return
    for p in bus.rows("payment"):
        if p.get("contributor_id") == ev.row["id"] and p.get("status") == "awaiting_onboarding":
            _log_outcome(executor.handle(p, ev.row, _episode(bus, p)))
