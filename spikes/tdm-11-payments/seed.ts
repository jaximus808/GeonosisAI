// TDM-11 TEST ONLY: copied into a throwaway module copy by local_test.sh, never
// published to maincloud. Contributors have no reducer yet (TDM-60), so the
// test seeds one; everything else goes through the real reducers.
import { t } from 'spacetimedb/server';
import spacetimedb from '../schema';
import { requireOwner } from '../auth';

export const tdm11_test_seed_contributor = spacetimedb.reducer({ name: t.string() }, (ctx, { name }) => {
  requireOwner(ctx);
  ctx.db.contributor.insert({
    id: 0n,
    identity: undefined,
    kind: 'contributor',
    name,
    devices: ['android_webxr'],
    online: true,
    stripe_account_id: undefined,
    payouts_enabled: false,
    total_paid_cents: 0n,
    created_at: ctx.timestamp,
  });
});
