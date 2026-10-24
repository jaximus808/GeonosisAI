# Stripe plan: contributor and dataset payouts (TEST MODE ONLY)

Payments for "Dispatch and pay" in `CONTEXT.md`: the factory pays contributors only after the reviewer accepts an episode, and pays dataset sellers only after QA accepts a batch. All of this runs in **Stripe test mode**. No real money moves. The dashboard must show a visible **"Stripe TEST MODE: no real money"** badge wherever we show payments.

The plan follows the Stripe `connect-recommend` and `stripe-best-practices` skills. The Stripe MCP `stripe_implementation_planner` was not used because its OAuth step was never completed. Run it later to double-check this plan.

## Chosen flow

| Decision | Choice | Why |
|---|---|---|
| Product | Stripe Connect, Accounts v2 (`/v2/core/accounts`) | The platform pays many third parties. v2 is Stripe's current Connect API; legacy `type: express` is deprecated. |
| Connected account config | `dashboard: "express"`, `fees_collector: "application"`, `losses_collector: "application"`, `configuration.recipient` with `stripe_balance.stripe_transfers` | Contributors and sellers only *receive* money. They never take card payments, so no `merchant` config is needed, which keeps onboarding short. Express gives them a Stripe-hosted earnings page. This is the only combination Stripe accepts for recipient accounts on this platform. |
| Funds flow | **Transfers from the platform balance** (the "separate charges and transfers" pattern, minus the per-payout charge) | A payout happens long after any money comes in, and only after a review decision. That is hold-and-release behaviour, which destination charges can't do. Each accepted episode becomes one `Transfer`. |
| Onboarding | Stripe-hosted onboarding with v2 Account Links (`use_case.type = account_onboarding`, `configurations: ["recipient"]`) | Stripe collects KYC. The phone task page shows "Set up payouts" until `stripe_transfers` is `active`. |
| Platform money (test) | Test balance, topped up with a `pm_card_bypassPending` PaymentIntent | Funds land in the *available* balance right away, so transfers work. Normal test charges stay *pending*. |
| Budget funding (optional) | Checkout Session (`mode: payment`) for the ticket owner, credited from the `checkout.session.completed` webhook | Shows "owner funds a $50 budget". Not needed for payouts, because the budget is enforced in SpacetimeDB either way. |
| Platform fee | None | The factory is the buyer, not a marketplace taking a cut. `application_fee_amount` is not used, and it can't be used with transfers anyway. |

## Stripe objects used

- **v2 Account** (`acct_...`): one per contributor or dataset seller. `metadata.rdf_contributor_id` links it back to the SpacetimeDB `contributor.id`. Created idempotently with the key `rdf-acct-<kind>-<contributor_id>`.
- **v2 Account Link**: a single-use onboarding URL that expires in minutes. Mint a fresh one every time someone clicks "Set up payouts".
- **Login Link** (v1): opens the contributor's Express dashboard.
- **Transfer** (`tr_...`): one per accepted episode, or one per accepted dataset batch.
  - Idempotency key: `rdf-episode-<episode_id>` or `rdf-dataset-<purchase_id>`.
  - `transfer_group`: `ticket_<ticket_id>`.
  - Metadata: episode, contributor, ticket and task type.
- **Transfer Reversal**: claws back a payout if an accepted episode is later found bad.
- **PaymentIntent** (test top-up) and **Checkout Session** (budget funding).
- **Balance**: checked before paying out.

## Code (`services/payments/`, uv project `rdf-payments`, package `rdf_payments`)

| File | Contents |
|---|---|
| `src/rdf_payments/stripe_client.py` | Shared `StripeClient`, pinned to API version `2026-09-30.endive`. It **refuses any key that isn't `sk_test_`/`rk_test_`** and never logs keys. |
| `src/rdf_payments/payments.py` | Payout functions (listed below the table). |
| `src/rdf_payments/webhooks.py` | Signature-verified event parsing and dispatch. |
| `src/rdf_payments/smoke_test.py` | End-to-end test-mode check (`uv run rdf-payments-smoke`; `services/payments/smoke_test.py` is a shim for the old command). |
| `pyproject.toml` | `rdf-common`, `stripe==16.0.0`, `python-dotenv`. The orchestrator depends on it by path. |

Functions in `payments.py`:

- `create_contributor_account(contributor_id, display_name, email, kind=...)`
- `onboarding_link(account_id, return_url, refresh_url)`
- `transfers_status(account_id)`
- `dashboard_login_link(account_id)`
- `get_balance()`
- `top_up_test_balance(cents)`
- `pay_for_episode(account, episode_id, amount_cents, review_accepted=..., budget_cents=..., spent_cents=...)`
- `pay_reserved(account, ref, amount_cents, ...)`: transfer an amount a `payment` row already reserved (used by the orchestrator for every row kind).
- `find_transfer(account, ref)`: durable dedupe. Every transfer carries its idempotency key as `metadata.rdf_ref`, and `_transfer` returns an existing match instead of paying again, so retries stay safe after Stripe's 24h key window.
- `pay_for_dataset(...)`
- `dataset_billable_episodes(...)`
- `reverse_payment(transfer_id)`
- `create_budget_checkout(ticket_id, cents, ...)`
- `check_budget(...)`

Setup:

```bash
cd services/payments && uv sync
uv run rdf-payments-smoke                          # reads ../../.env
uv run rdf-payments-smoke --account acct_... --wait
```

## Orchestrator payout handler (TDM-63)

`services/orchestrator/src/rdf_orchestrator/handlers/payments.py` runs on the bus: `on_enter('payment', status=='pending')`.

- **No `stripe_account_id`, or `payouts_enabled=false`:** calls `mark_payment_awaiting_onboarding`. The payment is **not** failed and the budget stays reserved.
- **Otherwise:** makes one Transfer through `pay_reserved`, keyed `rdf-episode-<episode_id>`, `rdf-task-<task_id>` or `rdf-dataset-<offer>-<batch_ref>`, then calls `mark_payment_paid(id, 'stripe_test', 'tr_...')`.
- **Errors:**
  - account not transfer-capable: `awaiting_onboarding`;
  - low platform test balance: one `pm_card_bypassPending` top-up, then retry;
  - network, 5xx or rate limit: retried with backoff, then failed;
  - any other Stripe refusal: `mark_payment_failed`.
- **When a contributor becomes payable** (`on_enter('contributor', stripe_account_id and payouts_enabled)`): their `awaiting_onboarding` rows are paid.
- **Account setup:** `set_contributor_payout_account(contributor_id, stripe_account_id, payouts_enabled)` (reducer, `spacetime/spacetimedb/src/reducers/payouts.ts`, payments or orchestrator role) sets the account fields.

HTTP routes (`rdf_orchestrator/payments_api.py`):

| Route | What it does |
|---|---|
| `POST /payments/contributors/{id}/onboarding` | Creates the account if needed and returns a fresh onboarding URL. Returns 409 until loss liability is accepted. |
| `POST /payments/contributors/{id}/refresh` | Re-checks `stripe_transfers` and sets `payouts_enabled`. |
| `POST /stripe/webhook` | Handles `transfer.created`, `transfer.reversed` and `account.updated`. |
| `GET /payments/mode` | Returns the TEST MODE badge text. |

Tests:

- `services/orchestrator/tests/test_tdm63_payments.py` uses a **mocked** Stripe.
- `spacetime/tests/test_tdm63_payout_flow.py` runs the real reducers on a local server with a **mocked** Stripe.

## Mapping to the SpacetimeDB `payment` table

`payment (id, contributor_id, episode_id, amount_cents, provider, status)`. Two columns should be added: **`provider_ref`** (the Stripe transfer id) and **`ticket_id`**.

| Column | Value |
|---|---|
| `id` | Our own id. Create the row *before* calling Stripe. |
| `contributor_id` | `contributor.id`. Look up the `acct_...` through a new `contributor.stripe_account_id` column. |
| `episode_id` | The episode id. For dataset purchases, use the purchase/batch id (or add a `dataset_purchase_id` column). |
| `amount_cents` | `task.price_cents`. Example prices are in `PRICES_CENTS`: reset 200, recovery_takeover 500, clean_teleop 1000. |
| `provider` | `"stripe_test"` |
| `status` | `pending`, then `paid` (transfer created) or `failed` (with an error reason). `reversed` after a reversal or a `transfer.reversed` event. Also `blocked_budget` and `awaiting_onboarding`. |

**Payout sequence** (run by the reviewer, or by the orchestrator once a review is written):

1. The reviewer writes `review(accepted=true)`.
2. The `reserve_payment(episode_id)` reducer does all of this **atomically**:
   - checks that the review is accepted;
   - checks that no `payment` row exists yet for this episode (unique on `episode_id`);
   - checks that `ticket.spent_cents + amount <= ticket.budget_cents`;
   - inserts `payment(status=pending)`, increments `ticket.spent_cents`, and appends to `event_log`.

   Doing this in one reducer is the real budget guard, because parallel reviewers can't overspend.
3. The orchestrator calls `pay_for_episode(...)`, passing `budget_cents`/`spent_cents` again. `check_budget` is a second guard that runs before any API call.
4. `mark_payment_paid(payment_id, provider, provider_ref)` sets the row to `paid` and updates `contributor.total_paid_cents`. `mark_payment_failed` sets `failed` and releases the reserved spend; `mark_payment_awaiting_onboarding` keeps it reserved until the contributor can be paid (TDM-11 reducers).
5. If the call fails, retry with the same `episode_id`. Stripe's idempotency key returns the same transfer for 24h, and after that the `payment` row is the dedupe.

## Budget guard and spend approval

- The owner sets `ticket.budget_cents` (for example 5000). Payouts can never exceed it, because the reducer refuses and so does `check_budget`.
- The planner's `data_plan` (data type × amount × price) is the *spend request*. It needs approval before the dispatcher creates paid tasks, through either:
  - the dashboard **Approve spend** button, or
  - the MCP connector's `approve_spend(ticket_id, data_plan_id)`.

  Both call one reducer, `approve_spend`, which sets `data_plan.approved_cents` (≤ remaining budget) and logs who approved it.
- The dispatcher only creates paid tasks while the approved amount covers them. Payouts check both `approved_cents` and `budget_cents`.
- Rejected episodes never reach step 2, so they are not paid. The dashboard shows the rejection reason instead.

## Dataset purchase payouts

- A seller offers N episodes. The factory asks for N total but already holds `already_uploaded` of them, so at most `N - already_uploaded` are billable.
- QA agents review the delivered episodes. Billable = `min(N - already_uploaded, qa_accepted)`. This is `dataset_billable_episodes()`; for example, N=50, uploaded=12, accepted 40 gives 38.
- Payout = `billable × price_per_episode_cents`, sent as one transfer per delivered batch through `pay_for_dataset(..., qa_accepted=True)`. It is idempotent on `purchase_id`, so a partial second delivery uses a new id such as `<purchase>-batch2`. The same reserve/budget reducer runs first.
- Sellers are the same kind of v2 recipient account, with `kind="dataset_seller"` in metadata.

## Webhooks

The transfer path itself is synchronous: `transfers.create` either returns a transfer or raises. We still need webhooks for:

- `checkout.session.completed` / `checkout.session.async_payment_succeeded`, to credit a funded budget. Ignore the event while `payment_status == "unpaid"`.
- Account capability changes (thin v2 events, `v2.core.account[configuration.recipient].capability_status_updated`), to mark a contributor payable.
- `transfer.reversed`, to set `payment.status = reversed`.

`webhooks.py` verifies signatures, and it was tested with a signed and a forged payload. For local dev, run `stripe listen --forward-to localhost:<port>/stripe/webhook`. Put the printed `whsec_...` in `.env` as `STRIPE_WEBHOOK_SECRET` with a single-line replace, and never print it. That isn't done yet, because no webhook endpoint is running. Wire it in before demoing Checkout funding.

## Test-mode setup status and manual steps

1. **Connect is enabled** on the sandbox. This was done with `stripe tools enable_connect`, test only.
2. **MANUAL, blocking:** in the Stripe Dashboard *for this sandbox*, open **Settings → Connect → Platform profile** (`https://dashboard.stripe.com/acct_1UTycMPt66MyJKMN/settings/connect/platform-profile`) and complete the **loss-liability acknowledgement** and any verification it asks for. Until then, Stripe rejects every recipient account with "platform as the losses collector" (`account_creation_liability_unacknowledged`), for both v2 and v1.
3. Re-run the smoke test (`cd services/payments && uv run rdf-payments-smoke`). It creates a test account and prints an onboarding link. Open the link and fill it with test data:
   - phone `000 000 0000`, SMS code `000000`;
   - SSN `000-00-0000`, DOB `01/01/1901`, address line `address_full_match`;
   - test bank routing `110000000`, account `000123456789`.

   Then re-run with `--account acct_... --wait`. The script transfers $5 and prints `PASS transfer_id=tr_... status=paid`.
4. The platform test balance is already funded with available funds (top-up PaymentIntent `pm_card_bypassPending`). The smoke test tops up again whenever less than $5 is available.

## Risks and notes

- `losses_collector: application` means the platform is liable for connected-account negative balances. With transfer-only payouts the only source of those is our own reversals. In test mode this has no financial effect.
- Payouts from connected accounts to their bank use Stripe's default schedule. Nothing extra is needed for the demo.
- For the live "judge's phone receives a paid task" moment, onboard one judge or demo account ahead of time, because onboarding takes about a minute with test data.
- Prefer a restricted key (`rk_test_`) with only Connect, Transfers, Balance, PaymentIntents and Checkout permissions once things stabilise. `stripe_client.py` already accepts `rk_test_`.
