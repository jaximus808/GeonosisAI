"""End-to-end TEST MODE smoke test for factory payouts.

    services/payments/.venv/bin/python services/payments/smoke_test.py [--account acct_...] [--wait]

Steps:
  1. Refuse unless STRIPE_SECRET_KEY is an sk_test_ key.
  2. Check the budget guard and dataset math offline.
  3. Make sure the platform has >= $5 available test balance (tops up with
     pm_card_bypassPending if not).
  4. Create a test connected account (Accounts v2, Express, recipient), or reuse
     --account / $STRIPE_SMOKE_ACCOUNT.
  5. If its transfers capability isn't active yet, print a Stripe-hosted onboarding
     link (test mode: use test data, SSN 000-00-0000, phone 000-000-0000, code 000000);
     with --wait, poll until it's active.
  6. pay_for_episode($5) with a ticket budget of $50, then repeat the call to prove
     idempotency (same transfer id), and print the transfer id and status.

Exit codes: 0 pass, 1 failure, 2 Dashboard setup needed, 3 onboarding needed.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.payments import (  # noqa: E402
    BudgetExceeded,
    PlatformNotConfigured,
    check_budget,
    create_contributor_account,
    dataset_billable_episodes,
    get_balance,
    onboarding_link,
    pay_for_episode,
    top_up_test_balance,
    transfers_status,
)
from services.payments.stripe_client import LiveModeRefused, get_client  # noqa: E402

AMOUNT = 500  # $5 recovery takeover
BUDGET = 5000  # $50 ticket budget

# Stripe test-mode identity values (https://docs.stripe.com/connect/testing).
TEST_IDENTITY = {
    "given_name": "Test",
    "surname": "Contributor",
    "email": "smoke-contributor@example.com",
    "phone": "+14155550100",
    "date_of_birth": {"day": 1, "month": 1, "year": 1901},
    "address": {
        "line1": "address_full_match",
        "city": "San Francisco",
        "state": "CA",
        "postal_code": "94103",
        "country": "us",
    },
    "id_numbers": [{"type": "us_ssn", "value": "000000000"}],
}


def step(msg: str) -> None:
    print(f"[smoke] {msg}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default=os.environ.get("STRIPE_SMOKE_ACCOUNT"))
    ap.add_argument("--wait", action="store_true", help="poll until onboarding completes")
    ap.add_argument("--episode-id", default=None)
    args = ap.parse_args()

    try:
        get_client()
    except LiveModeRefused as err:
        step(f"REFUSED: {err}")
        return 1
    step("using a Stripe TEST MODE key")

    # Offline checks.
    assert check_budget(BUDGET, 4500, 500) == 0
    try:
        check_budget(BUDGET, 4600, 500)
        raise AssertionError("budget guard should have refused")
    except BudgetExceeded:
        pass
    assert dataset_billable_episodes(n_requested=50, n_already_uploaded=12, n_accepted=40) == 38
    assert dataset_billable_episodes(n_requested=50, n_already_uploaded=12, n_accepted=30) == 30
    step("budget guard and dataset math OK")

    bal = get_balance()
    step(f"platform test balance: available {bal['available']}c, pending {bal['pending']}c")
    if bal["available"] < AMOUNT:
        pi = top_up_test_balance(2000)
        bal = get_balance()
        step(f"topped up with {pi}; available now {bal['available']}c")

    account = args.account
    if account:
        step(f"reusing connected account {account}")
    else:
        try:
            account = create_contributor_account(
                f"smoke-{uuid.uuid4().hex[:8]}",
                "Smoke Test Contributor",
                TEST_IDENTITY["email"],
                prefill_identity=TEST_IDENTITY,
            )
        except PlatformNotConfigured as err:
            step(f"SETUP NEEDED: {err}")
            return 2
        step(f"created connected account {account}")

    status = transfers_status(account)
    step(f"stripe_transfers capability: {status}")
    if status != "active":
        url = onboarding_link(
            account,
            return_url="https://example.com/rdf/onboarding/return",
            refresh_url="https://example.com/rdf/onboarding/refresh",
        )
        step("finish TEST-mode onboarding for this account (use Stripe test data):")
        print(url, flush=True)
        if not args.wait:
            step(f"then re-run with: --account {account}")
            return 3
        deadline = time.time() + 15 * 60
        while status != "active" and time.time() < deadline:
            time.sleep(5)
            status = transfers_status(account)
        step(f"stripe_transfers capability: {status}")
        if status != "active":
            return 3

    episode_id = args.episode_id or f"smoke-ep-{uuid.uuid4().hex[:12]}"
    result = pay_for_episode(
        account,
        episode_id,
        AMOUNT,
        contributor_id="smoke-contributor",
        ticket_id="smoke-ticket",
        task_type="recovery_takeover",
        review_accepted=True,
        budget_cents=BUDGET,
        spent_cents=0,
    )
    again = pay_for_episode(
        account, episode_id, AMOUNT, contributor_id="smoke-contributor", ticket_id="smoke-ticket",
        task_type="recovery_takeover", budget_cents=BUDGET, spent_cents=0,
    )
    assert again.provider_ref == result.provider_ref, "idempotency failed: paid twice"
    step(f"idempotent retry returned the same transfer ({again.provider_ref})")

    print(
        f"PASS transfer_id={result.provider_ref} status={result.status} amount={result.amount_cents}c "
        f"destination={result.destination} episode_id={episode_id} provider={result.provider}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
