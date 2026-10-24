"""TDM-63 payout handler tests.

MOCKED: every test here uses a fake Stripe (``MockStripe``) and fake reducers. No Stripe API call
and no SpacetimeDB call is made. The real test-mode transfer is blocked until the platform accepts
Connect loss liability in the Stripe Dashboard (docs/morning-questions.md #8); after that, the
real path is ``cd services/payments && uv run rdf-payments-smoke``.
"""
from __future__ import annotations

import asyncio

import pytest

from rdf_orchestrator import payments_api
from rdf_orchestrator.bus import Bus, Registry
from rdf_orchestrator.handlers import payments as ph
from rdf_orchestrator.stdb import Row


# --------------------------------------------------------------------------- fakes (MOCKED)


class StripeFail(Exception):
    def __init__(self, kind: str, msg: str = "boom"):
        super().__init__(msg)
        self.kind = kind


class MockStripe:
    """MOCKED Stripe: same-ref calls return the same transfer, like a Stripe idempotency key."""

    def __init__(self, fail: list[str] | None = None):
        self.fail = list(fail or [])
        self.calls: list[dict] = []
        self.transfers: dict[str, str] = {}
        self.topups: list[int] = []

    def pay(self, destination, ref, amount_cents, *, payee_id, ticket_id, description, metadata):
        self.calls.append({"destination": destination, "ref": ref, "amount": amount_cents,
                           "ticket_id": ticket_id, "metadata": metadata, "description": description})
        if self.fail:
            raise StripeFail(self.fail.pop(0))
        return self.transfers.setdefault(ref, f"tr_MOCK_{len(self.transfers) + 1}")

    def top_up(self, amount_cents):
        self.topups.append(amount_cents)
        return "pi_MOCK_topup"

    def classify(self, err):
        return err.kind if isinstance(err, StripeFail) else "permanent"


class ReducerError(Exception):
    def __init__(self, status: int):
        super().__init__(f"reducer failed ({status})")
        self.status = status


class FakeReducers:
    def __init__(self, fail_statuses: list[int] | None = None):
        self.calls: list[tuple] = []
        self.fail_statuses = list(fail_statuses or [])

    def call(self, reducer, *args):
        if self.fail_statuses:
            raise ReducerError(self.fail_statuses.pop(0))
        self.calls.append((reducer, *args))


def make(fail=None, reducer_fail=None):
    st, rd = MockStripe(fail), FakeReducers(reducer_fail)
    return ph.PayoutExecutor(lambda: st, lambda: rd, sleep=lambda s: None), st, rd


def pay_row(**kw):
    base = dict(id=11, ticket_id=3, contributor_id=5, episode_id=42, dataset_offer_id=0, batch_ref=None,
                amount_cents=500, provider="stripe_test", provider_ref=None, status="pending", error=None)
    return Row({**base, **kw})


def contributor(**kw):
    base = dict(id=5, kind="contributor", name="Ada", stripe_account_id="acct_TEST123", payouts_enabled=True,
                total_paid_cents=0)
    return Row({**base, **kw})


# --------------------------------------------------------------------------- executor


def test_paid_path_mocked_stripe_reports_transfer_id():
    ex, st, rd = make()
    o = ex.handle(pay_row(), contributor())
    assert o.result == "paid" and o.provider_ref == "tr_MOCK_1"
    assert st.calls[0]["ref"] == "rdf-episode-42"  # idempotency key from docs/stripe-plan.md
    assert st.calls[0]["destination"] == "acct_TEST123" and st.calls[0]["amount"] == 500
    assert st.calls[0]["metadata"]["rdf_payment_id"] == "11" and st.calls[0]["ticket_id"] == "3"
    assert rd.calls == [("mark_payment_paid", 11, "stripe_test", "tr_MOCK_1")]


@pytest.mark.parametrize("c", [contributor(stripe_account_id=None, payouts_enabled=False),
                               contributor(payouts_enabled=False)])
def test_unonboarded_contributor_goes_awaiting_onboarding_not_failed(c):
    ex, st, rd = make()
    o = ex.handle(pay_row(), c)
    assert o.result == "awaiting_onboarding"
    assert st.calls == []
    assert [r[0] for r in rd.calls] == ["mark_payment_awaiting_onboarding"] and rd.calls[0][1] == 11


def test_awaiting_row_without_account_reports_nothing_again():
    ex, st, rd = make()
    o = ex.handle(pay_row(status="awaiting_onboarding"), contributor(stripe_account_id=None, payouts_enabled=False))
    assert o.result == "awaiting_onboarding" and rd.calls == [] and st.calls == []


def test_awaiting_row_is_paid_once_contributor_is_payable():
    ex, st, rd = make()
    o = ex.handle(pay_row(status="awaiting_onboarding"), contributor())
    assert o.result == "paid" and rd.calls == [("mark_payment_paid", 11, "stripe_test", "tr_MOCK_1")]


def test_stripe_says_account_not_ready_goes_awaiting():
    ex, st, rd = make(fail=["not_ready"])
    assert ex.handle(pay_row(), contributor()).result == "awaiting_onboarding"
    assert [r[0] for r in rd.calls] == ["mark_payment_awaiting_onboarding"]


def test_low_platform_balance_tops_up_once_then_pays():
    ex, st, rd = make(fail=["low_balance"])
    assert ex.handle(pay_row(), contributor()).result == "paid"
    assert st.topups == [500 + ph.TOPUP_BUFFER_CENTS] and len(st.calls) == 2


def test_low_balance_twice_fails():
    ex, st, rd = make(fail=["low_balance", "low_balance"])
    assert ex.handle(pay_row(), contributor()).result == "failed"
    assert rd.calls[0][0] == "mark_payment_failed"


def test_transient_errors_retry_then_pay_and_retry_keeps_same_key():
    ex, st, rd = make(fail=["transient", "transient"])
    assert ex.handle(pay_row(), contributor()).result == "paid"
    assert {c["ref"] for c in st.calls} == {"rdf-episode-42"} and len(st.calls) == 3


def test_transient_forever_fails_and_permanent_fails_immediately():
    ex, st, rd = make(fail=["transient"] * 10)
    assert ex.handle(pay_row(), contributor()).result == "failed"
    assert len(st.calls) == 1 + len(ph.TRANSIENT_BACKOFF_S)
    ex, st, rd = make(fail=["permanent"])
    o = ex.handle(pay_row(), contributor())
    assert o.result == "failed" and len(st.calls) == 1
    assert rd.calls[0][0] == "mark_payment_failed" and "permanent" in rd.calls[0][2]


@pytest.mark.parametrize("status", ["paid", "failed", "blocked_budget", "reversed"])
def test_non_payable_statuses_are_skipped(status):
    ex, st, rd = make()
    assert ex.handle(pay_row(status=status), contributor()).result == "skipped"
    assert st.calls == [] and rd.calls == []


def test_rejected_episode_is_never_paid():
    """The reducer never creates a payment row for a rejected review; this guards it anyway."""
    ex, st, rd = make()
    o = ex.handle(pay_row(), contributor(), Row(id=42, review_status="rejected"))
    assert o.result == "failed" and st.calls == []
    assert rd.calls[0][0] == "mark_payment_failed"


def test_refire_is_idempotent_same_transfer():
    """Boot snapshot / resync re-fires a still-pending row: the same transfer comes back."""
    ex, st, rd = make()
    ex.handle(pay_row(), contributor())
    ex.handle(pay_row(), contributor())  # e.g. mark_payment_paid never reached the DB the first time
    assert len(st.transfers) == 1
    assert rd.calls == [("mark_payment_paid", 11, "stripe_test", "tr_MOCK_1")] * 2  # reducer no-ops on same ref


def test_report_retries_server_errors_and_raises_on_refusal():
    ex, st, rd = make(reducer_fail=[503])
    assert ex.handle(pay_row(), contributor()).result == "paid"  # one 503, then reported
    ex, st, rd = make(reducer_fail=[409])
    with pytest.raises(ReducerError):
        ex.handle(pay_row(), contributor())  # left pending for the next fire, transfer already exists


def test_payout_refs():
    assert ph.payout_ref(pay_row()) == "rdf-episode-42"
    assert ph.payout_ref(pay_row(episode_id=0, batch_ref="task:9")) == "rdf-task-9"
    assert ph.payout_ref(pay_row(episode_id=0, dataset_offer_id=4, batch_ref="b1")) == "rdf-dataset-4-b1"


def test_missing_contributor_fails():
    ex, st, rd = make()
    assert ex.handle(pay_row(), None).result == "failed" and st.calls == []


# --------------------------------------------------------------------------- bus wiring (MOCKED)


class FakeClient:
    def __init__(self):
        self.state, self.identity, self.cb = "stopped", "0xfake", {}
        self._stop = asyncio.Event()

    def subscribe(self, q): self.queries = q
    def on_snapshot(self, cb): self.cb["snapshot"] = cb
    def on_insert(self, cb): self.cb["insert"] = cb
    def on_update(self, cb): self.cb["update"] = cb
    def on_delete(self, cb): self.cb["delete"] = cb
    def on_state(self, cb): self.cb["state"] = cb
    def key(self, table, row): return row["id"]
    async def run(self): await self._stop.wait()
    def stop(self): self._stop.set()
    async def call_reducer(self, r, a): return 1


async def _settle(bus, n=100):
    for _ in range(n):
        await asyncio.sleep(0.01)
        if bus.health()["inflight"] == 0:
            return


def test_bus_flow_pending_awaiting_then_paid_after_onboarding(monkeypatch):
    ex, st, rd = make()
    monkeypatch.setattr(ph, "executor", ex)
    reg, fc = Registry(), FakeClient()
    reg.on_enter("payment", where=lambda r: r.status == "pending")(ph.payout_pending)
    reg.on_enter("contributor", where=ph._payable)(ph.contributor_payable)
    reg.watch("episode")
    bus = Bus(reg, fc, workers=2)

    async def go():
        await bus.start()
        fc.cb["state"]("live", None)
        fc.cb["snapshot"]({
            "contributor": [contributor(stripe_account_id=None, payouts_enabled=False)],
            # a rejected episode has no payment row at all (TDM-11), so nothing fires for it
            "episode": [Row(id=42, review_status="accepted"), Row(id=43, review_status="rejected")],
            "payment": [pay_row()],
        })
        await _settle(bus)
        assert [r[0] for r in rd.calls] == ["mark_payment_awaiting_onboarding"]
        # the reducer moves the row; the contributor then finishes onboarding
        fc.cb["update"]("payment", pay_row(), pay_row(status="awaiting_onboarding"), {})
        fc.cb["update"]("contributor", contributor(stripe_account_id=None, payouts_enabled=False), contributor(), {})
        await _settle(bus)
        await bus.stop()

    asyncio.run(go())
    assert rd.calls[-1] == ("mark_payment_paid", 11, "stripe_test", "tr_MOCK_1")
    assert len(st.calls) == 1 and st.calls[0]["ref"] == "rdf-episode-42"


# --------------------------------------------------------------------------- webhook mapping (MOCKED)


def test_webhook_transfer_reversed_and_created(monkeypatch):
    ex, st, rd = make()
    monkeypatch.setattr(ph, "executor", ex)
    rows = [pay_row(id=1, status="paid", provider_ref="tr_A"), pay_row(id=2, episode_id=77)]
    assert payments_api.apply_event("transfer.reversed", {"id": "tr_A"}, rows) == "reversed:1"
    assert payments_api.apply_event("transfer.created", {"id": "tr_B", "metadata": {"rdf_ref": "rdf-episode-77"}},
                                    rows) == "paid:2"
    assert payments_api.apply_event("transfer.created", {"id": "tr_C", "metadata": {}}, rows) is None
    assert rd.calls == [("mark_payment_reversed", 1, "stripe transfer.reversed"),
                        ("mark_payment_paid", 2, "stripe_test", "tr_B")]
