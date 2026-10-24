// TDM-12 local test only: run_local.sh copies this into a throwaway copy of the
// module. It is NEVER published to maincloud (it would let the owner fabricate
// episodes). Seeds one ticket, 3 accepted episodes and 1 pending episode.
import spacetimedb from '../schema';
import { requireOwner } from '../auth';

export const tdm12_test_seed = spacetimedb.reducer((ctx) => {
  requireOwner(ctx);
  const ticket = ctx.db.ticket.insert({
    id: 0n,
    robot_id: 0n,
    task_description: 'test.tdm12 pick up the cup',
    target_success: 0.8,
    budget_cents: 10_000n,
    approved_cents: 0n,
    spent_cents: 0n,
    status: 'train',
    dataset_id: 0n,
    iteration: 1,
    deployed_checkpoint_uri: undefined,
    created_by: 'test.tdm12',
    created_at: ctx.timestamp,
    updated_at: ctx.timestamp,
  });
  for (const review_status of ['accepted', 'accepted', 'accepted', 'pending']) {
    ctx.db.episode.insert({
      id: 0n,
      ticket_id: ticket.id,
      robot_id: 0n,
      task_id: 0n,
      contributor_id: 0n,
      dataset_offer_id: 0n,
      kind: 'clean_teleop',
      review_status,
      checkpoint_uri: undefined,
      storage_uri: 'test://tdm12/episode',
      cameras_json: '{}',
      actions_uri: 'test://tdm12/actions',
      frame_count: 100,
      duration_ms: 5000,
      takeover_ts: undefined,
      handback_ts: undefined,
      success: true,
      created_at: ctx.timestamp,
    });
  }
});
