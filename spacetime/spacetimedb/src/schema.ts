// Robot Deployment Factory: all tables (TDM-8). Reducers live in index.ts and
// in the reducer tickets' own files (TDM-9..15); this file only holds tables.
//
// Conventions (see spacetime/schema.md):
// - Every id is u64 auto-inc; ids start at 1, so a u64 foreign key of 0n means
//   "none". Indexed foreign keys that can be absent use that sentinel because
//   option columns cannot be btree-filtered from clients.
// - Enumerations are plain strings; the allowed values live in enums.ts and
//   reducers must validate them. (Strings keep the HTTP/JSON API simple for
//   the Python services.)
// - *_json columns hold JSON text; *_uri columns point to object storage.
// - Every table that the dashboard/robot/phones subscribe to is public. Only
//   service_identity (the reducer allow-list) is private.
import { schema, table, t, type InferSchema, type ReducerCtx } from 'spacetimedb/server';

// ---------------------------------------------------------------- auth

// Private allow-list of service identities. The owner is inserted by `init`.
// Reducers that matter call requireService/requireRole (auth.ts).
const serviceIdentity = table(
  { name: 'service_identity' },
  {
    identity: t.identity().primaryKey(),
    role: t.string(), // enums.ts SERVICE_ROLES
    label: t.string(),
    added_at: t.timestamp(),
  }
);

// ---------------------------------------------------------------- robots & tickets

const robot = table(
  { name: 'robot', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    name: t.string(),
    embodiment: t.string(), // e.g. "i2rt_yam"
    status: t.string().index('btree'), // ROBOT_STATUSES
    adapter_version: t.string(),
    camera_config_json: t.string(),
    client_identity: t.option(t.identity()), // Jetson robot-client identity
    last_heartbeat: t.option(t.timestamp()),
    created_at: t.timestamp(),
  }
);

const ticket = table(
  { name: 'ticket', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    robot_id: t.u64().index('btree'),
    task_description: t.string(),
    target_success: t.f64(),
    budget_cents: t.u64(), // hard cap; reserve_payment refuses beyond it
    approved_cents: t.u64(), // sum of data_plan.approved_cents (approve_spend)
    spent_cents: t.u64(), // reserved + paid payouts (reserve_payment increments)
    status: t.string().index('btree'), // TICKET_STATUSES (flywheel stage)
    dataset_id: t.u64(), // 0n until the dataset row exists
    iteration: t.u32(), // flywheel loop count, starts at 0 (baseline)
    deployed_checkpoint_uri: t.option(t.string()),
    created_by: t.string(), // "web" | "mcp" | ...
    created_at: t.timestamp(),
    updated_at: t.timestamp(),
  }
);

// One row per ticket. Defaults in enums.ts DEFAULT_RUN_SETTINGS.
const runSettings = table(
  { name: 'run_settings', public: true },
  {
    ticket_id: t.u64().primaryKey(),
    eval_trials: t.u32(),
    success_threshold: t.f64(),
    require_ci_low: t.bool(),
    budget_cents: t.u64(),
    target_episodes: t.u32(),
    max_iterations: t.u32(),
    hyperparams_json: t.string(), // {lr, steps, batch_size, lora_rank, prompt}
    updated_at: t.timestamp(),
  }
);

// Per-ticket training dataset (accepted episodes only go to training).
const dataset = table(
  { name: 'dataset', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    ticket_id: t.u64().unique(),
    accepted_count: t.u32(),
    total_count: t.u32(),
    lerobot_uri: t.option(t.string()),
    updated_at: t.timestamp(),
  }
);

// ---------------------------------------------------------------- planning

const failureCluster = table(
  { name: 'failure_cluster', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    ticket_id: t.u64().index('btree'),
    iteration: t.u32(),
    label: t.string(), // e.g. "drops mid-grasp"
    description: t.string(),
    count: t.u32(),
    example_episode_ids: t.array(t.u64()),
    created_at: t.timestamp(),
  }
);

const dataPlan = table(
  { name: 'data_plan', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    ticket_id: t.u64().index('btree'),
    iteration: t.u32(),
    data_type: t.string(), // DATA_TYPES
    amount: t.u32(), // episodes requested
    unit_price_cents: t.u64(),
    est_cost_cents: t.u64(),
    reasoning: t.string(), // planner's logged reasoning (must be real)
    failure_cluster_ids: t.array(t.u64()),
    status: t.string().index('btree'), // DATA_PLAN_STATUSES
    approved_cents: t.u64(), // set by approve_spend, <= remaining budget
    approved_by: t.option(t.string()),
    approved_at: t.option(t.timestamp()),
    created_at: t.timestamp(),
  }
);

// ---------------------------------------------------------------- marketplace

const task = table(
  { name: 'task', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    ticket_id: t.u64().index('btree'),
    robot_id: t.u64().index('btree'),
    data_plan_id: t.u64(), // 0n for resets not tied to a plan
    type: t.string(), // TASK_TYPES
    status: t.string().index('btree'), // TASK_STATUSES
    instructions: t.string(),
    price_cents: t.u64(),
    assignee_id: t.u64().index('btree'), // contributor.id, 0n when unassigned
    claimed_at: t.option(t.timestamp()),
    lease_expires_at: t.option(t.timestamp()),
    completed_at: t.option(t.timestamp()),
    created_at: t.timestamp(),
  }
);

const contributor = table(
  { name: 'contributor', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    identity: t.option(t.identity()), // phone/web identity; none for sellers created by the orchestrator
    kind: t.string(), // CONTRIBUTOR_KINDS
    name: t.string(),
    devices: t.array(t.string()), // e.g. ["android_webxr", "leader_arm"]
    online: t.bool(),
    stripe_account_id: t.option(t.string()), // acct_... (test mode); not a secret
    payouts_enabled: t.bool(), // Stripe recipient capability active
    total_paid_cents: t.u64(),
    created_at: t.timestamp(),
  }
);

// ---------------------------------------------------------------- episodes & review

const episode = table(
  { name: 'episode', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    ticket_id: t.u64().index('btree'),
    robot_id: t.u64().index('btree'),
    task_id: t.u64(), // 0n if none
    contributor_id: t.u64(), // 0n if none (baseline/eval rollouts)
    dataset_offer_id: t.u64(), // 0n unless kind = purchased
    kind: t.string().index('btree'), // EPISODE_KINDS
    review_status: t.string().index('btree'), // REVIEW_STATUSES
    checkpoint_uri: t.option(t.string()), // policy that drove the rollout
    storage_uri: t.string(),
    cameras_json: t.string(), // {camera_name: mp4_uri}
    actions_uri: t.string(), // parquet/npz of IK targets + joint positions
    frame_count: t.u32(),
    duration_ms: t.u32(),
    takeover_ts: t.option(t.timestamp()),
    handback_ts: t.option(t.timestamp()),
    success: t.option(t.bool()),
    created_at: t.timestamp(),
  }
);

const review = table(
  { name: 'review', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    episode_id: t.u64().index('btree'),
    ticket_id: t.u64().index('btree'),
    accepted: t.bool(),
    reason: t.string(),
    metrics_json: t.string(),
    reviewer: t.string(), // agent name
    created_at: t.timestamp(),
  }
);

// Deterministic reviewer/QA checks, one row per check.
const qaResult = table(
  { name: 'qa_result', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    episode_id: t.u64().index('btree'),
    check_name: t.string(), // e.g. "jerk", "frame_drops", "takeover_ts"
    passed: t.bool(),
    detail_json: t.string(),
    created_at: t.timestamp(),
  }
);

// Row created by reserve_payment BEFORE calling Stripe (docs/stripe-plan.md).
const payment = table(
  { name: 'payment', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    ticket_id: t.u64().index('btree'),
    contributor_id: t.u64().index('btree'),
    episode_id: t.u64().index('btree'), // 0n for dataset batch payouts; reserve_payment enforces one per episode
    dataset_offer_id: t.u64(), // 0n unless a dataset purchase
    batch_ref: t.option(t.string()), // e.g. "<offer>-batch2"
    amount_cents: t.u64(),
    provider: t.string(), // "stripe_test"
    provider_ref: t.option(t.string()), // Stripe transfer id tr_...
    status: t.string().index('btree'), // PAYMENT_STATUSES
    error: t.option(t.string()),
    created_at: t.timestamp(),
    updated_at: t.timestamp(),
  }
);

const datasetOffer = table(
  { name: 'dataset_offer', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    ticket_id: t.u64().index('btree'),
    seller_id: t.u64(), // contributor.id with kind = dataset_seller
    source_uri: t.option(t.string()),
    price_per_episode_cents: t.u64(),
    needed_count: t.u32(),
    ingested_count: t.u32(),
    accepted_count: t.u32(),
    status: t.string().index('btree'), // DATASET_OFFER_STATUSES
    created_at: t.timestamp(),
    updated_at: t.timestamp(),
  }
);

// ---------------------------------------------------------------- training & eval

const trainingRun = table(
  { name: 'training_run', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    ticket_id: t.u64().index('btree'),
    iteration: t.u32(),
    episode_ids: t.array(t.u64()),
    status: t.string().index('btree'), // TRAINING_STATUSES
    modal_call_id: t.option(t.string()),
    base_checkpoint_uri: t.string(),
    checkpoint_uri: t.option(t.string()),
    hyperparams_json: t.string(),
    metrics_json: t.string(),
    gpu_cost_cents: t.u64(),
    error: t.option(t.string()),
    created_at: t.timestamp(),
    started_at: t.option(t.timestamp()),
    finished_at: t.option(t.timestamp()),
  }
);

const evalRun = table(
  { name: 'eval_run', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    ticket_id: t.u64().index('btree'),
    robot_id: t.u64().index('btree'),
    training_run_id: t.u64(), // 0n for the baseline
    kind: t.string(), // EVAL_KINDS
    checkpoint_uri: t.string(),
    status: t.string().index('btree'), // EVAL_STATUSES
    trials: t.u32(), // planned
    completed: t.u32(),
    successes: t.u32(),
    ci_low: t.f64(), // Wilson 95%
    ci_high: t.f64(),
    created_at: t.timestamp(),
    finished_at: t.option(t.timestamp()),
  }
);

const evalTrial = table(
  { name: 'eval_trial', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    eval_run_id: t.u64().index('btree'),
    trial_index: t.u32(),
    success: t.bool(),
    duration_ms: t.u32(),
    episode_id: t.u64().index('btree'), // 0n if not recorded
    failure_note: t.option(t.string()),
    created_at: t.timestamp(),
  }
);

// ---------------------------------------------------------------- agents & robot bus

// Long-running agents (Factory droids, planner, reviewer). The resume bridge
// watches status = "waiting_for" and resumes when the predicate holds.
const agentSession = table(
  { name: 'agent_session', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    ticket_id: t.u64().index('btree'), // 0n if not tied to a ticket
    role: t.string(), // AGENT_ROLES
    name: t.string(),
    factory_session_id: t.option(t.string()), // current session
    factory_session_ids_json: t.string(), // all sessions, JSON array (end-and-restart fallback)
    status: t.string().index('btree'), // AGENT_STATUSES
    current_step: t.string(),
    waiting_for_json: t.option(t.string()), // {table, predicate, ...}
    resume_payload_json: t.option(t.string()),
    created_at: t.timestamp(),
    last_update: t.timestamp(),
  }
);

// Commands for the Jetson robot-client. It subscribes by robot_id + status.
const robotCommand = table(
  { name: 'robot_command', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    robot_id: t.u64().index('btree'),
    ticket_id: t.u64().index('btree'), // 0n if none
    type: t.string(), // ROBOT_COMMAND_TYPES
    payload_json: t.string(),
    status: t.string().index('btree'), // ROBOT_COMMAND_STATUSES
    issued_by: t.string(),
    result_json: t.option(t.string()),
    issued_at: t.timestamp(),
    acked_at: t.option(t.timestamp()),
    finished_at: t.option(t.timestamp()),
    seq: t.u64().default(0n), // TDM-13: per-robot command sequence, 1, 2, 3...
  }
);

// Append-only evidence log. Columns unchanged from the placeholder module.
const eventLog = table(
  { name: 'event_log', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    ts: t.timestamp(),
    actor: t.string(),
    type: t.string(),
    payload_json: t.string(),
  }
);

const spacetimedb = schema({
  serviceIdentity,
  robot,
  ticket,
  runSettings,
  dataset,
  failureCluster,
  dataPlan,
  task,
  contributor,
  episode,
  review,
  qaResult,
  payment,
  datasetOffer,
  trainingRun,
  evalRun,
  evalTrial,
  agentSession,
  robotCommand,
  eventLog,
});
export default spacetimedb;

export type RdfCtx = ReducerCtx<InferSchema<typeof spacetimedb>>;
