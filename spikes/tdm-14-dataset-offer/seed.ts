// TEST ONLY (local throwaway server): seed rows that other tickets' reducers would create.
import { t } from 'spacetimedb/server';
import spacetimedb from '../schema';
export const tdm14_seed = spacetimedb.reducer({ target: t.u32(), accepted: t.u32() }, (ctx, { target, accepted }) => {
  const r = ctx.db.robot.insert({ id: 0n, name: 'test', embodiment: 'i2rt_yam', status: 'online', adapter_version: '', camera_config_json: '{}', client_identity: undefined, last_heartbeat: undefined, created_at: ctx.timestamp });
  const tk = ctx.db.ticket.insert({ id: 0n, robot_id: r.id, task_description: 'test.cups', target_success: 0.8, budget_cents: 5000n, approved_cents: 0n, spent_cents: 0n, status: 'dispatch', dataset_id: 0n, iteration: 0, deployed_checkpoint_uri: undefined, created_by: 'test', created_at: ctx.timestamp, updated_at: ctx.timestamp });
  ctx.db.runSettings.insert({ ticket_id: tk.id, eval_trials: 10, success_threshold: 0.8, require_ci_low: false, budget_cents: 5000n, target_episodes: target, max_iterations: 3, hyperparams_json: '{}', updated_at: ctx.timestamp });
  ctx.db.dataset.insert({ id: 0n, ticket_id: tk.id, accepted_count: accepted, total_count: accepted, lerobot_uri: undefined, updated_at: ctx.timestamp });
  for (let i = 0; i < accepted; i++) ctx.db.episode.insert({ id: 0n, ticket_id: tk.id, robot_id: r.id, task_id: 0n, contributor_id: 0n, dataset_offer_id: 0n, kind: 'clean_teleop', review_status: 'accepted', checkpoint_uri: undefined, storage_uri: 's3://test/e' + i, cameras_json: '{}', actions_uri: '', frame_count: 100, duration_ms: 1000, takeover_ts: undefined, handback_ts: undefined, success: true, created_at: ctx.timestamp });
  ctx.db.contributor.insert({ id: 0n, identity: undefined, kind: 'dataset_seller', name: 'test.seller', devices: [], online: false, stripe_account_id: undefined, payouts_enabled: false, total_paid_cents: 0n, created_at: ctx.timestamp });
  ctx.db.contributor.insert({ id: 0n, identity: undefined, kind: 'contributor', name: 'test.person', devices: [], online: false, stripe_account_id: undefined, payouts_enabled: false, total_paid_cents: 0n, created_at: ctx.timestamp });
});
// Stand-in for TDM-11 submit_review: writes a review row + episode.review_status.
export const tdm14_test_review = spacetimedb.reducer({ episode_id: t.u64(), accepted: t.bool() }, (ctx, { episode_id, accepted }) => {
  const ep = ctx.db.episode.id.find(episode_id)!;
  ctx.db.review.insert({ id: 0n, episode_id, ticket_id: ep.ticket_id, accepted, reason: accepted ? 'ok' : 'bad', metrics_json: '{}', reviewer: 'test', created_at: ctx.timestamp });
  ctx.db.episode.id.update({ ...ep, review_status: accepted ? 'accepted' : 'rejected' });
});
// Stand-in for TDM-11 create_episode: a pending clean_teleop upload from a contributor.
export const tdm14_test_upload = spacetimedb.reducer({ ticket_id: t.u64() }, (ctx, { ticket_id }) => {
  const tk = ctx.db.ticket.id.find(ticket_id)!;
  ctx.db.episode.insert({ id: 0n, ticket_id, robot_id: tk.robot_id, task_id: 0n, contributor_id: 2n, dataset_offer_id: 0n, kind: 'clean_teleop', review_status: 'pending', checkpoint_uri: undefined, storage_uri: 's3://test/upload', cameras_json: '{}', actions_uri: '', frame_count: 100, duration_ms: 1000, takeover_ts: undefined, handback_ts: undefined, success: true, created_at: ctx.timestamp });
});
