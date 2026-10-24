// TEST-ONLY (TDM-10 local race harness). Never copied into the real module.
// Registered reducer names are snake-cased by the SDK: tdm_10_test_grant, etc.
// Lets each throwaway identity grant itself a role / become a contributor, and
// seeds a robot + ticket, so the harness needs no owner token.
import { t } from 'spacetimedb/server';
import spacetimedb from './schema';

export const tdm10_test_grant = spacetimedb.reducer({ role: t.string() }, (ctx, { role }) => {
  ctx.db.serviceIdentity.insert({ identity: ctx.sender, role, label: 'tdm10 test', added_at: ctx.timestamp });
});

export const tdm10_test_add_contributor = spacetimedb.reducer({ name: t.string() }, (ctx, { name }) => {
  ctx.db.contributor.insert({
    id: 0n, identity: ctx.sender, kind: 'contributor', name, devices: ['android_webxr'],
    online: true, stripe_account_id: undefined, payouts_enabled: false, total_paid_cents: 0n,
    created_at: ctx.timestamp,
  });
});

export const tdm10_test_seed_ticket = spacetimedb.reducer({ robot_status: t.string() }, (ctx, { robot_status }) => {
  const robot = ctx.db.robot.insert({
    id: 0n, name: 'test-yam', embodiment: 'i2rt_yam', status: robot_status, adapter_version: 'test',
    camera_config_json: '{}', client_identity: undefined, last_heartbeat: undefined, created_at: ctx.timestamp,
  });
  ctx.db.ticket.insert({
    id: 0n, robot_id: robot.id, task_description: 'test.tdm10', target_success: 0.8, budget_cents: 5000n,
    approved_cents: 0n, spent_cents: 0n, status: 'dispatch', dataset_id: 0n, iteration: 0,
    deployed_checkpoint_uri: undefined, created_by: 'test', created_at: ctx.timestamp, updated_at: ctx.timestamp,
  });
});
