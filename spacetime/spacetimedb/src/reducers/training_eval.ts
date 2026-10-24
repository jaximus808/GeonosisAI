// TDM-12: training, eval, failure-cluster and data-plan reducers.
//
// Every reducer here checks ctx.sender (auth.ts) and appends one event_log row.
// Only reducers are exported (index.ts re-exports this file with `export *`).
//
// Invariants this file enforces:
// - training_run: queued -> running -> done | failed | cancelled. Terminal rows
//   never change. Only accepted episodes of the same ticket can be trained on,
//   and a ticket has at most one queued/running training run at a time.
// - eval_run: trials are recorded one per trial_index (0-based, < trials) while
//   the run is `running`. finalize_eval_run runs exactly once: it is rejected
//   unless every planned trial row exists, and the successes it is given must
//   match the stored trial rows (the orchestrator computes the Wilson interval,
//   the module refuses numbers the trial rows don't back up).
// - data_plan: insert-only. Nothing in this file (or anywhere else) may edit a
//   plan's type, amount, prices, reasoning or cluster ids after creation: the
//   row is the evidence of what the planner decided on its own. approve_spend
//   (another ticket) may set only status, approved_cents, approved_by and
//   approved_at. The full evidence_json is kept in the plan's event_log row.
import { t, SenderError } from 'spacetimedb/server';
import spacetimedb, { type RdfCtx } from '../schema';
import { requireRole } from '../auth';
import { DEFAULT_PRICES_CENTS, DEFAULT_RUN_SETTINGS, TRAINING_STATUSES, oneOf } from '../enums';

// openpi base weights (spikes/tdm-36-finetune/rdf_s05a_config.py PI05_BASE_PARAMS).
const TDM12_PI05_BASE_CHECKPOINT = 'gs://openpi-assets/checkpoints/pi05_base/params';
const TDM12_PLAN_DATA_TYPES = ['clean_teleop', 'recovery_takeover'] as const;
const TDM12_EPS = 1e-9;

function tdm12Log(ctx: RdfCtx, actor: string, type: string, payload: unknown): void {
  ctx.db.eventLog.insert({
    id: 0n,
    ts: ctx.timestamp,
    actor,
    type,
    payload_json: JSON.stringify(payload, (_k, v) => (typeof v === 'bigint' ? Number(v) : v)),
  });
}

function tdm12Ticket(ctx: RdfCtx, ticketId: bigint) {
  const ticket = ctx.db.ticket.id.find(ticketId);
  if (!ticket) throw new SenderError(`unknown ticket ${ticketId}`);
  return ticket;
}

function tdm12Json(field: string, text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    throw new SenderError(`${field} is not valid JSON`);
  }
}

function tdm12JsonObject(field: string, text: string): Record<string, unknown> {
  const v = tdm12Json(field, text);
  if (v === null || typeof v !== 'object' || Array.isArray(v)) {
    throw new SenderError(`${field} must be a JSON object`);
  }
  return v as Record<string, unknown>;
}

/** Parses a JSON array of distinct positive integer ids. */
function tdm12Ids(field: string, text: string): bigint[] {
  const v = tdm12Json(field, text);
  if (!Array.isArray(v)) throw new SenderError(`${field} must be a JSON array of ids`);
  const ids: bigint[] = [];
  for (const x of v) {
    if (typeof x !== 'number' || !Number.isSafeInteger(x) || x <= 0) {
      throw new SenderError(`${field} must contain positive integer ids, got ${JSON.stringify(x)}`);
    }
    const id = BigInt(x);
    if (ids.includes(id)) throw new SenderError(`${field} has duplicate id ${x}`);
    ids.push(id);
  }
  return ids;
}

function tdm12TicketEpisode(ctx: RdfCtx, ticketId: bigint, episodeId: bigint) {
  const ep = ctx.db.episode.id.find(episodeId);
  if (!ep) throw new SenderError(`unknown episode ${episodeId}`);
  if (ep.ticket_id !== ticketId) {
    throw new SenderError(`episode ${episodeId} belongs to ticket ${ep.ticket_id}, not ${ticketId}`);
  }
  return ep;
}

function tdm12NonEmpty(field: string, value: string): string {
  const v = value.trim();
  if (!v) throw new SenderError(`${field} must not be empty`);
  return v;
}

// ---------------------------------------------------------------- training

// Queue a fine-tune on accepted episodes. hyperparams_json "" uses the
// ticket's run_settings (or the defaults); an optional base_checkpoint_uri key
// overrides the pi0.5 base weights.
export const queue_training_run = spacetimedb.reducer(
  { ticket_id: t.u64(), episode_ids_json: t.string(), hyperparams_json: t.string() },
  (ctx, { ticket_id, episode_ids_json, hyperparams_json }) => {
    const role = requireRole(ctx, 'orchestrator', 'factory');
    const ticket = tdm12Ticket(ctx, ticket_id);

    const episodeIds = tdm12Ids('episode_ids_json', episode_ids_json);
    if (episodeIds.length === 0) throw new SenderError('episode_ids_json must list at least one episode');
    for (const id of episodeIds) {
      const ep = tdm12TicketEpisode(ctx, ticket_id, id);
      if (ep.review_status !== 'accepted') {
        throw new SenderError(`episode ${id} is ${ep.review_status}; only accepted episodes are trained on`);
      }
    }

    const active = [...ctx.db.trainingRun.ticket_id.filter(ticket_id)].find(
      (r) => r.status === 'queued' || r.status === 'running'
    );
    if (active) throw new SenderError(`ticket ${ticket_id} already has training run ${active.id} ${active.status}`);

    const hpText = hyperparams_json.trim()
      ? hyperparams_json
      : (ctx.db.runSettings.ticket_id.find(ticket_id)?.hyperparams_json ?? DEFAULT_RUN_SETTINGS.hyperparams_json);
    const hp = tdm12JsonObject('hyperparams_json', hpText);
    const base =
      typeof hp.base_checkpoint_uri === 'string' && hp.base_checkpoint_uri.trim()
        ? hp.base_checkpoint_uri.trim()
        : TDM12_PI05_BASE_CHECKPOINT;

    const run = ctx.db.trainingRun.insert({
      id: 0n,
      ticket_id,
      iteration: ticket.iteration,
      episode_ids: episodeIds,
      status: 'queued',
      modal_call_id: undefined,
      base_checkpoint_uri: base,
      checkpoint_uri: undefined,
      hyperparams_json: JSON.stringify(hp),
      metrics_json: '{}',
      gpu_cost_cents: 0n,
      error: undefined,
      created_at: ctx.timestamp,
      started_at: undefined,
      finished_at: undefined,
    });
    tdm12Log(ctx, role, 'training.queued', {
      training_run_id: run.id,
      ticket_id,
      iteration: ticket.iteration,
      episode_ids: episodeIds,
      base_checkpoint_uri: base,
      hyperparams: hp,
    });
  }
);

const TDM12_TRAINING_NEXT: Record<string, readonly string[]> = {
  queued: ['queued', 'running', 'failed', 'cancelled'],
  running: ['running', 'done', 'failed', 'cancelled'],
};

// Move a training run along queued -> running -> done|failed|cancelled. A
// same-status call (queued->queued, running->running) just updates the given
// fields, e.g. modal_call_id once spawned or progress metrics. done requires a
// checkpoint_uri. For failed, an "error" string in metrics_json is copied to
// the error column. gpu_cost_cents replaces the stored value (total so far).
export const set_training_status = spacetimedb.reducer(
  {
    run_id: t.u64(),
    status: t.string(),
    modal_call_id: t.option(t.string()),
    checkpoint_uri: t.option(t.string()),
    metrics_json: t.option(t.string()),
    gpu_cost_cents: t.option(t.u64()),
  },
  (ctx, { run_id, status, modal_call_id, checkpoint_uri, metrics_json, gpu_cost_cents }) => {
    const role = requireRole(ctx, 'orchestrator', 'factory');
    oneOf('status', status, TRAINING_STATUSES);
    const run = ctx.db.trainingRun.id.find(run_id);
    if (!run) throw new SenderError(`unknown training run ${run_id}`);
    const allowed = TDM12_TRAINING_NEXT[run.status];
    if (!allowed) throw new SenderError(`training run ${run_id} is ${run.status} (terminal); it cannot change`);
    if (!allowed.includes(status)) {
      throw new SenderError(`training run ${run_id}: ${run.status} -> ${status} is not allowed`);
    }

    let metrics = run.metrics_json;
    let error = run.error;
    if (metrics_json !== undefined) {
      const m = tdm12JsonObject('metrics_json', metrics_json);
      metrics = JSON.stringify(m);
      if (status === 'failed' && typeof m.error === 'string' && m.error) error = m.error;
    }
    const ckpt = checkpoint_uri !== undefined && checkpoint_uri.trim() ? checkpoint_uri.trim() : run.checkpoint_uri;
    if (status === 'done' && !ckpt) throw new SenderError('status done requires checkpoint_uri');
    const terminal = status === 'done' || status === 'failed' || status === 'cancelled';

    ctx.db.trainingRun.id.update({
      ...run,
      status,
      modal_call_id: modal_call_id !== undefined && modal_call_id.trim() ? modal_call_id.trim() : run.modal_call_id,
      checkpoint_uri: ckpt,
      metrics_json: metrics,
      gpu_cost_cents: gpu_cost_cents ?? run.gpu_cost_cents,
      error,
      started_at: run.started_at ?? (status === 'running' ? ctx.timestamp : undefined),
      finished_at: terminal ? ctx.timestamp : run.finished_at,
    });
    tdm12Log(ctx, role, 'training.status', {
      training_run_id: run_id,
      ticket_id: run.ticket_id,
      from: run.status,
      to: status,
      modal_call_id: modal_call_id ?? null,
      checkpoint_uri: ckpt ?? null,
      gpu_cost_cents: gpu_cost_cents ?? run.gpu_cost_cents,
      error: error ?? null,
    });
  }
);

// ---------------------------------------------------------------- eval

// Start an N-trial eval of a checkpoint on the ticket's robot. trials 0 uses
// run_settings.eval_trials (default 10). If the checkpoint is the output of a
// finished training run for this ticket the eval is a `candidate` linked to
// that run, otherwise it is the `baseline`.
export const start_eval_run = spacetimedb.reducer(
  { ticket_id: t.u64(), checkpoint_uri: t.string(), trials: t.u32() },
  (ctx, { ticket_id, checkpoint_uri, trials }) => {
    const role = requireRole(ctx, 'orchestrator');
    const ticket = tdm12Ticket(ctx, ticket_id);
    const ckpt = tdm12NonEmpty('checkpoint_uri', checkpoint_uri);
    const n =
      trials > 0
        ? trials
        : (ctx.db.runSettings.ticket_id.find(ticket_id)?.eval_trials ?? DEFAULT_RUN_SETTINGS.eval_trials);
    if (n <= 0) throw new SenderError('trials must be > 0');

    const source = [...ctx.db.trainingRun.ticket_id.filter(ticket_id)].find(
      (r) => r.status === 'done' && r.checkpoint_uri === ckpt
    );
    const run = ctx.db.evalRun.insert({
      id: 0n,
      ticket_id,
      robot_id: ticket.robot_id,
      training_run_id: source?.id ?? 0n,
      kind: source ? 'candidate' : 'baseline',
      checkpoint_uri: ckpt,
      status: 'running',
      trials: n,
      completed: 0,
      successes: 0,
      ci_low: 0,
      ci_high: 0,
      created_at: ctx.timestamp,
      finished_at: undefined,
    });
    tdm12Log(ctx, role, 'eval.started', {
      eval_run_id: run.id,
      ticket_id,
      robot_id: ticket.robot_id,
      kind: run.kind,
      training_run_id: run.training_run_id,
      checkpoint_uri: ckpt,
      trials: n,
    });
  }
);

// One trial outcome. index is 0-based and must be < eval_run.trials, once per
// index. episode_id 0 = no recording; failure_note "" = none.
export const record_eval_trial = spacetimedb.reducer(
  {
    eval_run_id: t.u64(),
    index: t.u32(),
    success: t.bool(),
    duration_ms: t.u32(),
    episode_id: t.u64(),
    failure_note: t.string(),
  },
  (ctx, { eval_run_id, index, success, duration_ms, episode_id, failure_note }) => {
    const role = requireRole(ctx, 'orchestrator', 'robot_client');
    const run = ctx.db.evalRun.id.find(eval_run_id);
    if (!run) throw new SenderError(`unknown eval run ${eval_run_id}`);
    if (run.status !== 'running') throw new SenderError(`eval run ${eval_run_id} is ${run.status}, not running`);
    if (index >= run.trials) {
      throw new SenderError(`trial index ${index} out of range: eval run ${eval_run_id} has ${run.trials} trials (0-based)`);
    }
    const existing = [...ctx.db.evalTrial.eval_run_id.filter(eval_run_id)];
    if (existing.some((r) => r.trial_index === index)) {
      throw new SenderError(`eval run ${eval_run_id} already has trial ${index}`);
    }
    if (episode_id !== 0n) tdm12TicketEpisode(ctx, run.ticket_id, episode_id);
    const note = failure_note.trim() ? failure_note.trim() : undefined;

    const trial = ctx.db.evalTrial.insert({
      id: 0n,
      eval_run_id,
      trial_index: index,
      success,
      duration_ms,
      episode_id,
      failure_note: note,
      created_at: ctx.timestamp,
    });
    ctx.db.evalRun.id.update({
      ...run,
      completed: existing.length + 1,
      successes: existing.filter((r) => r.success).length + (success ? 1 : 0),
    });
    tdm12Log(ctx, role, 'eval.trial', {
      eval_run_id,
      eval_trial_id: trial.id,
      ticket_id: run.ticket_id,
      index,
      success,
      duration_ms,
      episode_id,
      failure_note: note ?? null,
    });
  }
);

// Called once by the orchestrator's EvalRunFinalizer (services/orchestrator
// stats.py) as finalize(eval_run_id, successes, trials, ci_low, ci_high,
// summary). Rejected if the run is already finalized, if fewer trial rows
// exist than planned, or if the numbers don't match the stored trials.
export const finalize_eval_run = spacetimedb.reducer(
  {
    eval_run_id: t.u64(),
    successes: t.u32(),
    trials: t.u32(),
    ci_low: t.f64(),
    ci_high: t.f64(),
    summary: t.string(),
  },
  (ctx, { eval_run_id, successes, trials, ci_low, ci_high, summary }) => {
    const role = requireRole(ctx, 'orchestrator');
    const run = ctx.db.evalRun.id.find(eval_run_id);
    if (!run) throw new SenderError(`unknown eval run ${eval_run_id}`);
    if (run.status === 'done') throw new SenderError(`eval run ${eval_run_id} is already finalized`);
    if (run.status !== 'running') throw new SenderError(`eval run ${eval_run_id} is ${run.status}, not running`);

    const rows = [...ctx.db.evalTrial.eval_run_id.filter(eval_run_id)];
    if (rows.length < run.trials) {
      throw new SenderError(`eval run ${eval_run_id} has ${rows.length} of ${run.trials} trial rows; cannot finalize`);
    }
    if (trials !== run.trials || trials !== rows.length) {
      throw new SenderError(`trials ${trials} does not match eval run ${eval_run_id} (${run.trials} planned, ${rows.length} recorded)`);
    }
    const actual = rows.filter((r) => r.success).length;
    if (successes !== actual) {
      throw new SenderError(`successes ${successes} does not match the ${actual} successful trial rows`);
    }
    const rate = successes / trials;
    const finite = Number.isFinite(ci_low) && Number.isFinite(ci_high);
    if (!finite || ci_low < -TDM12_EPS || ci_high > 1 + TDM12_EPS || ci_low > rate + TDM12_EPS || ci_high < rate - TDM12_EPS) {
      throw new SenderError(`CI [${ci_low}, ${ci_high}] must satisfy 0 <= ci_low <= ${rate} <= ci_high <= 1`);
    }

    ctx.db.evalRun.id.update({
      ...run,
      status: 'done',
      completed: rows.length,
      successes,
      ci_low,
      ci_high,
      finished_at: ctx.timestamp,
    });
    tdm12Log(ctx, role, 'eval.finalized', {
      eval_run_id,
      ticket_id: run.ticket_id,
      kind: run.kind,
      checkpoint_uri: run.checkpoint_uri,
      training_run_id: run.training_run_id,
      successes,
      trials,
      success_rate: rate,
      ci_low,
      ci_high,
      summary,
    });
  }
);

// ---------------------------------------------------------------- planning

// A diagnosed failure type for the ticket's current iteration.
export const record_failure_cluster = spacetimedb.reducer(
  { ticket_id: t.u64(), label: t.string(), count: t.u32(), example_episode_ids_json: t.string() },
  (ctx, { ticket_id, label, count, example_episode_ids_json }) => {
    const role = requireRole(ctx, 'orchestrator', 'factory');
    const ticket = tdm12Ticket(ctx, ticket_id);
    const name = tdm12NonEmpty('label', label);
    if (count === 0) throw new SenderError('count must be > 0');
    const examples = tdm12Ids('example_episode_ids_json', example_episode_ids_json);
    for (const id of examples) tdm12TicketEpisode(ctx, ticket_id, id);

    const row = ctx.db.failureCluster.insert({
      id: 0n,
      ticket_id,
      iteration: ticket.iteration,
      label: name,
      description: '',
      count,
      example_episode_ids: examples,
      created_at: ctx.timestamp,
    });
    tdm12Log(ctx, role, 'planner.failure_cluster', {
      failure_cluster_id: row.id,
      ticket_id,
      iteration: ticket.iteration,
      label: name,
      count,
      example_episode_ids: examples,
    });
  }
);

// The planner's decision. Insert-only (see the header). evidence_json is a
// JSON object; optional keys: failure_cluster_ids (this ticket's clusters) and
// unit_price_cents (default DEFAULT_PRICES_CENTS[data_type]). Everything in it
// is preserved in the event_log row.
export const record_data_plan = spacetimedb.reducer(
  { ticket_id: t.u64(), data_type: t.string(), amount: t.u32(), reasoning: t.string(), evidence_json: t.string() },
  (ctx, { ticket_id, data_type, amount, reasoning, evidence_json }) => {
    const role = requireRole(ctx, 'orchestrator', 'factory');
    const ticket = tdm12Ticket(ctx, ticket_id);
    const kind = oneOf('data_type', data_type, TDM12_PLAN_DATA_TYPES);
    if (amount === 0) throw new SenderError('amount must be > 0');
    const why = tdm12NonEmpty('reasoning', reasoning);
    const evidence = tdm12JsonObject('evidence_json', evidence_json.trim() ? evidence_json : '{}');

    const clusterIds = evidence.failure_cluster_ids === undefined
      ? []
      : tdm12Ids('evidence_json.failure_cluster_ids', JSON.stringify(evidence.failure_cluster_ids));
    for (const id of clusterIds) {
      const c = ctx.db.failureCluster.id.find(id);
      if (!c || c.ticket_id !== ticket_id) throw new SenderError(`failure cluster ${id} is not on ticket ${ticket_id}`);
    }

    let unit: bigint = DEFAULT_PRICES_CENTS[kind];
    if (evidence.unit_price_cents !== undefined) {
      const u = evidence.unit_price_cents;
      if (typeof u !== 'number' || !Number.isSafeInteger(u) || u < 0) {
        throw new SenderError('evidence_json.unit_price_cents must be a non-negative integer');
      }
      unit = BigInt(u);
    }
    const est = unit * BigInt(amount);

    const plan = ctx.db.dataPlan.insert({
      id: 0n,
      ticket_id,
      iteration: ticket.iteration,
      data_type: kind,
      amount,
      unit_price_cents: unit,
      est_cost_cents: est,
      reasoning: why,
      failure_cluster_ids: clusterIds,
      status: 'proposed',
      approved_cents: 0n,
      approved_by: undefined,
      approved_at: undefined,
      created_at: ctx.timestamp,
    });
    tdm12Log(ctx, role, 'planner.data_plan', {
      data_plan_id: plan.id,
      ticket_id,
      iteration: ticket.iteration,
      data_type: kind,
      amount,
      unit_price_cents: unit,
      est_cost_cents: est,
      budget_cents: ticket.budget_cents,
      approved_cents: ticket.approved_cents,
      spent_cents: ticket.spent_cents,
      reasoning: why,
      evidence,
    });
  }
);
