#!/usr/bin/env python3
"""TDM-63 end-to-end payout flow on a THROWAWAY local SpacetimeDB, with the REAL reducers and the
REAL orchestrator payout executor, but a MOCKED Stripe (no connected account can exist until the
platform accepts Connect loss liability; docs/morning-questions.md #8).

    spacetime start --listen-addr 127.0.0.1:3063 --data-dir spikes/tdm-63-payouts/tmp/stdb-data
    cd spacetime && spacetime publish -s http://127.0.0.1:3063 tdm63test --yes
    cd ../services/orchestrator && uv run python ../../spacetime/tests/test_tdm63_payout_flow.py

Flow: robot -> ticket ($50) -> task ($5 recovery_takeover) -> contributor claims + completes ->
episode -> submit_review(accept) reserves a pending payment -> executor marks it
awaiting_onboarding (no Stripe account) -> set_contributor_payout_account(acct, true) ->
executor pays (MOCKED transfer) -> mark_payment_paid. Also: a rejected episode gets no payment
row; set_contributor_payout_account auth / validation / idempotency.
"""
from __future__ import annotations

import argparse
import json
import sys

from rdf_common.spacetime_client import SpacetimeClient, SpacetimeError, create_identity, load_dotenv

from rdf_orchestrator.handlers.payments import PayoutExecutor

P = argparse.ArgumentParser()
P.add_argument("--host", default="http://127.0.0.1:3063")
P.add_argument("--db", default="tdm63test")
A = P.parse_args()
if "maincloud" in A.host:
    sys.exit("refusing to write test rows to maincloud; use a local server")
load_dotenv()
owner = SpacetimeClient(A.host, A.db)  # SPACETIME_TOKEN (the publisher = owner); never printed
ok = 0


def check(cond: bool, what: str) -> None:
    global ok
    if not cond:
        sys.exit(f"FAIL: {what}")
    ok += 1
    print(f"ok  {what}")


def one(q: str):
    rows = owner.sql(q)
    return rows[-1] if rows else None


def refused(client, reducer, *args) -> bool:
    try:
        client.call_reducer(reducer, *args)
        return False
    except SpacetimeError:
        return True


class MockStripe:  # MOCKED: stands in for rdf_payments.pay_reserved
    def __init__(self):
        self.calls = []

    def pay(self, destination, ref, amount_cents, **kw):
        self.calls.append((destination, ref, amount_cents))
        return "tr_MOCK_tdm63"

    def top_up(self, cents):
        raise AssertionError("no top-up expected")

    def classify(self, err):
        return "permanent"


class Reducers:
    def call(self, reducer, *args):
        owner.call_reducer(reducer, *args)


owner.call_reducer("register_robot", "tdm63-arm", "yam", "test", "{}")
robot = one("SELECT * FROM robot WHERE name = 'tdm63-arm'")
owner.call_reducer("create_ticket", robot["id"], "test.tdm63 payout flow", 0.8, 5000, "")
ticket = max(owner.sql("SELECT * FROM ticket"), key=lambda r: r["id"])

ident, tok = create_identity(A.host)
phone = SpacetimeClient(A.host, A.db, tok)
phone.call_reducer("register_contributor", "TDM63 Tester", "", ["android_webxr"], "contributor", 0)
contrib = max(owner.sql("SELECT * FROM contributor"), key=lambda r: r["id"])
cid, tid = contrib["id"], ticket["id"]


def paid_task_episode():
    owner.call_reducer("create_task", tid, "recovery_takeover", 500, json.dumps({"instructions": "take over"}))
    task = max(owner.sql(f"SELECT * FROM task WHERE ticket_id = {tid}"), key=lambda r: r["id"])
    phone.call_reducer("claim_task", task["id"], cid)
    phone.call_reducer("complete_task", task["id"], "{}")
    owner.call_reducer("create_episode", tid, task["id"], cid, "recovery_takeover", "file:///tmp/ep", "", "{}",
                       10, 1000, None, None, None, None)
    return max(owner.sql(f"SELECT * FROM episode WHERE ticket_id = {tid}"), key=lambda r: r["id"])


# rejected episode: no payment row, ever
ep_rej = paid_task_episode()
owner.call_reducer("submit_review", ep_rej["id"], False, "test.tdm63 blurry", "{}")
check(owner.sql(f"SELECT * FROM payment WHERE episode_id = {ep_rej['id']}") == [], "rejected episode has no payment row")

# accepted episode -> pending payment
ep = paid_task_episode()
owner.call_reducer("submit_review", ep["id"], True, "", "{}")
pay = one(f"SELECT * FROM payment WHERE episode_id = {ep['id']}")
check(pay is not None and pay["status"] == "pending" and pay["amount_cents"] == 500, "accept reserved a pending $5 payment")

stripe = MockStripe()
ex = PayoutExecutor(lambda: stripe, Reducers, sleep=lambda s: None)
c = one(f"SELECT * FROM contributor WHERE id = {cid}")
o = ex.handle(pay, c)
pay = one(f"SELECT * FROM payment WHERE id = {pay['id']}")
check(o.result == "awaiting_onboarding" and pay["status"] == "awaiting_onboarding" and stripe.calls == [],
      "no Stripe account -> awaiting_onboarding (not failed), no Stripe call")
check(one(f"SELECT * FROM ticket WHERE id = {tid}")["spent_cents"] == 500, "budget stays reserved while awaiting")

# set_contributor_payout_account: auth, validation, idempotency
check(refused(phone, "set_contributor_payout_account", cid, "acct_TEST1", True), "contributor cannot set own payout account")
check(refused(owner, "set_contributor_payout_account", cid, "not-an-acct", False), "bad account id refused")
check(refused(owner, "set_contributor_payout_account", cid, "", True), "payouts_enabled without account refused")
owner.call_reducer("set_contributor_payout_account", cid, "acct_TEST1", True)
n = len(owner.sql("SELECT * FROM event_log WHERE type = 'contributor.payout_account_updated'"))
owner.call_reducer("set_contributor_payout_account", cid, "acct_TEST1", True)
check(len(owner.sql("SELECT * FROM event_log WHERE type = 'contributor.payout_account_updated'")) == n == 1,
      "set_contributor_payout_account logs once and is idempotent")

c = one(f"SELECT * FROM contributor WHERE id = {cid}")
o = ex.handle(pay, c)
pay = one(f"SELECT * FROM payment WHERE id = {pay['id']}")
check(o.result == "paid" and pay["status"] == "paid" and pay["provider_ref"] == "tr_MOCK_tdm63"
      and pay["provider"] == "stripe_test", "payable contributor -> MOCKED transfer -> mark_payment_paid")
check(stripe.calls == [("acct_TEST1", f"rdf-episode-{ep['id']}", 500)], "transfer keyed rdf-episode-<id>")
check(one(f"SELECT * FROM contributor WHERE id = {cid}")["total_paid_cents"] == 500, "contributor total_paid_cents updated")
o = ex.handle(pay, c)
check(o.result == "skipped" and len(stripe.calls) == 1, "re-fire on a paid row is a no-op")
print(f"PASS {ok} checks (Stripe MOCKED; reducers real, local server)")
