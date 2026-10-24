// TDM-63: contributor payout account (Stripe Connect, TEST MODE).
//
// The orchestrator's payout handler (services/orchestrator/.../handlers/payments.py)
// pays a pending payment only when the contributor has a stripe_account_id and
// payouts_enabled. This reducer is how those two fields get set: after the
// orchestrator creates the connected account (POST /payments/contributors/<id>/onboarding)
// and whenever it re-checks the account's stripe_transfers capability.
// A contributor going payable makes the handler retry their awaiting_onboarding rows.
//
// Only reducers are exported (index.ts re-exports this file with `export *`).
import { t, SenderError } from 'spacetimedb/server';
import spacetimedb from '../schema';
import { requireRole } from '../auth';
import { logEvent } from '../event_log'; // TDM-15: the single event_log write path

const TDM63_ACCOUNT_RE = /^acct_[A-Za-z0-9]{1,64}$/;

// payments / orchestrator only. stripe_account_id "" clears the account (and
// then payouts_enabled must be false). Idempotent: an unchanged call is a no-op
// and logs nothing.
export const set_contributor_payout_account = spacetimedb.reducer(
  { contributor_id: t.u64(), stripe_account_id: t.string(), payouts_enabled: t.bool() },
  (ctx, { contributor_id, stripe_account_id, payouts_enabled }) => {
    const role = requireRole(ctx, 'payments', 'orchestrator');
    const c = ctx.db.contributor.id.find(contributor_id);
    if (!c) throw new SenderError(`contributor ${contributor_id} not found`);
    const acct = stripe_account_id.trim();
    if (acct !== '' && !TDM63_ACCOUNT_RE.test(acct)) {
      throw new SenderError('stripe_account_id must look like acct_...');
    }
    if (acct === '' && payouts_enabled) {
      throw new SenderError('payouts_enabled needs a stripe_account_id');
    }
    const next = acct === '' ? undefined : acct;
    if ((c.stripe_account_id ?? undefined) === next && c.payouts_enabled === payouts_enabled) return;
    ctx.db.contributor.id.update({ ...c, stripe_account_id: next, payouts_enabled });
    logEvent(ctx, role, 'contributor.payout_account_updated', {
      contributor_id,
      stripe_account_id: next ?? null,
      payouts_enabled,
      was: { stripe_account_id: c.stripe_account_id ?? null, payouts_enabled: c.payouts_enabled },
    });
  }
);
