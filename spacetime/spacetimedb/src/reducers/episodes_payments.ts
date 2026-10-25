// TDM-11: episode, review and payment reducers, plus approve_spend.
//
// Only reducers are exported (index.ts re-exports this file with `export *`);
// helpers are module-private and prefixed tdm11.
//
// Invariants this file enforces (docs/stripe-plan.md "Payout sequence"):
// - Reviews are final: one review row per episode, and only an episode whose
//   review_status is 'pending' can be reviewed. baseline/eval rollouts are
//   created 'not_reviewed' and never enter the dataset or the payout path.
// - A rejected review must carry a reason (shown on the earnings page) and is
//   never paid: no payment row is ever created for it.
// - A payment is reservable only once an accepted review exists for the
//   episode, and there is at most one payment per episode (and per task:
//   a second episode on the same task is refused while the first is
//   pending/accepted, and reservation refuses a task that is already paid).
// - Budget guard, atomic because a reducer is one serializable transaction:
//     ticket.spent_cents + GPU + amount <= ticket.budget_cents, always, where
//     GPU = sum of the ticket's training_run.gpu_cost_cents (TDM-42: the same
//     total the flywheel's budget stop uses; spent_cents stays payouts only);
//     and for a
//     task tied to a data plan, (that plan's reserved payouts) + amount <=
//     data_plan.approved_cents (set by approve_spend).
//   ticket.spent_cents is the sum of payments in a "reserved" status
//   (pending, awaiting_onboarding, paid). A payout over the cap is still
//   recorded, as status 'blocked_budget', without touching spent_cents, so the
//   dashboard can show it; reserve_payment retries it after more budget or
//   spend is approved.
// - Status machine: pending -> paid | failed | awaiting_onboarding;
//   awaiting_onboarding -> paid | failed; failed | blocked_budget -> pending
//   (re-reserve via reserve_*); paid -> reversed. failed and reversed release
//   the reserved spend. Stripe's idempotency key (rdf-episode-<id>) covers
//   the retry of a failed transfer.
// - Every state change appends one event_log row.
import { t, SenderError } from 'spacetimedb/server';
import spacetimedb, { type RdfCtx } from '../schema';
import { requireRole } from '../auth';
import { logEvent } from '../event_log'; // TDM-15
import { oneOf } from '../enums';

type Tdm11Ticket = NonNullable<ReturnType<RdfCtx['db']['ticket']['id']['find']>>;
type Tdm11Episode = NonNullable<ReturnType<RdfCtx['db']['episode']['id']['find']>>;
type Tdm11Payment = NonNullable<ReturnType<RdfCtx['db']['payment']['id']['find']>>;

// Who may call what (owner always passes requireRole).
const TDM11_EPISODE_WRITERS = ['robot_client', 'orchestrator', 'factory', 'web'];
const TDM11_REVIEWERS = ['reviewer', 'orchestrator'];
const TDM11_RESERVERS = ['reviewer', 'orchestrator', 'payments'];
const TDM11_PAYMENT_WRITERS = ['payments', 'orchestrator'];
const TDM11_SPEND_APPROVERS = ['web', 'mcp', 'orchestrator'];

// Episodes that are training data (go through review and into the dataset).
// 'purchased' episodes are created by ingest_purchased_episode (TDM-14).
const TDM11_DATASET_KINDS: readonly string[] = ['clean_teleop', 'recovery_takeover', 'purchased'];
const TDM11_CREATE_KINDS = ['baseline', 'eval', 'clean_teleop', 'recovery_takeover'] as const;
// Payment statuses that hold budget (counted in ticket.spent_cents).
const TDM11_RESERVED: readonly string[] = ['pending', 'awaiting_onboarding', 'paid'];
const TDM11_PROVIDER_DEFAULT = 'stripe_test';
const TDM11_TERMINAL_TICKET: readonly string[] = ['shipped', 'failed', 'cancelled'];
const TDM11_MAX_TEXT = 16_384;

function tdm11Json(value: unknown): string {
  return JSON.stringify(value, (_k, v) => (typeof v === 'bigint' ? v.toString() : v));
}

function tdm11Log(ctx: RdfCtx, actor: string, type: string, payload: Record<string, unknown>): void {
  logEvent(ctx, actor, type, tdm11Json({ ...payload, sender: ctx.sender.toHexString() })); // TDM-15: sole event_log writer
}

function tdm11JsonText(field: string, text: string): string {
  const s = text.trim() === '' ? '{}' : text;
  if (s.length > TDM11_MAX_TEXT) throw new SenderError(`${field} too large`);
  try {
    JSON.parse(s);
  } catch {
    throw new SenderError(`${field} is not valid JSON`);
  }
  return s;
}

function tdm11Ticket(ctx: RdfCtx, ticket_id: bigint): Tdm11Ticket {
  const ticket = ctx.db.ticket.id.find(ticket_id);
  if (!ticket) throw new SenderError(`ticket ${ticket_id} not found`);
  return ticket;
}

function tdm11Episode(ctx: RdfCtx, episode_id: bigint): Tdm11Episode {
  const ep = ctx.db.episode.id.find(episode_id);
  if (!ep) throw new SenderError(`episode ${episode_id} not found`);
  return ep;
}

function tdm11Payment(ctx: RdfCtx, payment_id: bigint): Tdm11Payment {
  const p = ctx.db.payment.id.find(payment_id);
  if (!p) throw new SenderError(`payment ${payment_id} not found`);
  return p;
}

/** The per-ticket dataset row; created (and linked on the ticket) if missing. */
function tdm11Dataset(ctx: RdfCtx, ticket: Tdm11Ticket) {
  const ds = ctx.db.dataset.ticket_id.find(ticket.id);
  if (ds) return ds;
  const created = ctx.db.dataset.insert({
    id: 0n,
    ticket_id: ticket.id,
    accepted_count: 0,
    total_count: 0,
    lerobot_uri: undefined,
    updated_at: ctx.timestamp,
  });
  if (ticket.dataset_id === 0n) {
    ctx.db.ticket.id.update({ ...ticket, dataset_id: created.id, updated_at: ctx.timestamp });
  }
  return created;
}

/** Sum of reserved payouts for tasks under one data plan. */
function tdm11PlanReserved(ctx: RdfCtx, ticket_id: bigint, data_plan_id: bigint, skip_id: bigint): bigint {
  let sum = 0n;
  for (const p of ctx.db.payment.ticket_id.filter(ticket_id)) {
    if (p.id === skip_id || !TDM11_RESERVED.includes(p.status) || p.episode_id === 0n) continue;
    const ep = ctx.db.episode.id.find(p.episode_id);
    if (!ep || ep.task_id === 0n) continue;
    const task = ctx.db.task.id.find(ep.task_id);
    if (task && task.data_plan_id === data_plan_id) sum += p.amount_cents;
  }
  return sum;
}

interface Tdm11Reserve {
  ticket_id: bigint;
  contributor_id: bigint;
  episode_id: bigint;
  dataset_offer_id: bigint;
  batch_ref: string | undefined;
  amount_cents: bigint;
  data_plan_id: bigint; // 0n = not tied to a plan (resets, dataset batches)
  existing: Tdm11Payment | undefined; // a failed / blocked_budget row to retry
  detail: Record<string, unknown>;
}

/**
 * The budget guard. Inserts (or re-reserves) a payment row as 'pending' and
 * adds the amount to ticket.spent_cents, or records 'blocked_budget' without
 * touching spent_cents when a cap would be exceeded.
 */
function tdm11Reserve(ctx: RdfCtx, actor: string, r: Tdm11Reserve): Tdm11Payment {
  const ticket = tdm11Ticket(ctx, r.ticket_id);
  if (r.amount_cents === 0n) throw new SenderError('payment amount must be > 0');

  let blocked: string | undefined;
  const after = ticket.spent_cents + r.amount_cents;
  const gpu = tdm42GpuCents(ctx, ticket.id);
  if (after + gpu > ticket.budget_cents) {
    blocked =
      `budget: spent ${ticket.spent_cents} + GPU ${gpu} + ${r.amount_cents} = ${after + gpu} ` +
      `> budget_cents ${ticket.budget_cents} on ticket ${ticket.id}`;
  } else if (r.data_plan_id !== 0n) {
    const plan = ctx.db.dataPlan.id.find(r.data_plan_id);
    const approved = plan ? plan.approved_cents : 0n;
    const reserved = tdm11PlanReserved(ctx, ticket.id, r.data_plan_id, r.existing ? r.existing.id : 0n);
    if (reserved + r.amount_cents > approved) {
      blocked =
        `approved spend: data_plan ${r.data_plan_id} has ${reserved} reserved + ${r.amount_cents} ` +
        `> approved_cents ${approved} (call approve_spend)`;
    }
  }
  const status = blocked ? 'blocked_budget' : 'pending';

  const fields = {
    ticket_id: r.ticket_id,
    contributor_id: r.contributor_id,
    episode_id: r.episode_id,
    dataset_offer_id: r.dataset_offer_id,
    batch_ref: r.batch_ref,
    amount_cents: r.amount_cents,
    provider: TDM11_PROVIDER_DEFAULT,
    provider_ref: undefined,
    status,
    error: blocked,
    updated_at: ctx.timestamp,
  };
  const row = r.existing
    ? ctx.db.payment.id.update({ ...r.existing, ...fields })
    : ctx.db.payment.insert({ id: 0n, ...fields, created_at: ctx.timestamp });
  if (!blocked) {
    ctx.db.ticket.id.update({ ...ticket, spent_cents: after, updated_at: ctx.timestamp });
  }
  tdm11Log(ctx, actor, blocked ? 'payment.blocked_budget' : 'payment.reserved', {
    payment_id: row.id,
    ticket_id: r.ticket_id,
    contributor_id: r.contributor_id,
    episode_id: r.episode_id,
    dataset_offer_id: r.dataset_offer_id,
    batch_ref: r.batch_ref,
    data_plan_id: r.data_plan_id,
    amount_cents: r.amount_cents,
    retry_of_status: r.existing?.status,
    spent_cents_before: ticket.spent_cents,
    spent_cents_after: blocked ? ticket.spent_cents : after,
    gpu_cost_cents: gpu,
    budget_cents: ticket.budget_cents,
    reason: blocked,
    ...r.detail,
  });
  return row;
}

/** TDM-42: GPU spend on the ticket (sum of training_run.gpu_cost_cents), as the flywheel counts it. */
function tdm42GpuCents(ctx: RdfCtx, ticket_id: bigint): bigint {
  let total = 0n;
  for (const run of ctx.db.trainingRun.ticket_id.filter(ticket_id)) total += run.gpu_cost_cents;
  return total;
}

/** Releases reserved spend (failed / reversed). Saturates at 0. */
function tdm11Release(ctx: RdfCtx, ticket_id: bigint, amount: bigint): void {
  const ticket = ctx.db.ticket.id.find(ticket_id);
  if (!ticket) return;
  const spent = ticket.spent_cents > amount ? ticket.spent_cents - amount : 0n;
  ctx.db.ticket.id.update({ ...ticket, spent_cents: spent, updated_at: ctx.timestamp });
}

/**
 * Reserve the payout for an accepted, task-backed episode. Returns quietly
 * when there is nothing to pay (no task, unpriced task) or the payment is
 * already reserved/paid, so it is safe to call more than once.
 */
function tdm11ReserveEpisode(ctx: RdfCtx, actor: string, ep: Tdm11Episode): void {
  const accepted = [...ctx.db.review.episode_id.filter(ep.id)].some((rv) => rv.accepted);
  if (!accepted) throw new SenderError(`episode ${ep.id} has no accepted review; it cannot be paid`);
  if (ep.task_id === 0n) return; // baseline / purchased (paid per batch) / unpaid recordings
  const task = ctx.db.task.id.find(ep.task_id);
  if (!task) throw new SenderError(`task ${ep.task_id} of episode ${ep.id} not found`);
  if (task.price_cents === 0n) return;

  const existing = [...ctx.db.payment.episode_id.filter(ep.id)][0];
  if (existing && existing.status !== 'failed' && existing.status !== 'blocked_budget') {
    if (existing.status === 'reversed') {
      throw new SenderError(`payment ${existing.id} for episode ${ep.id} was reversed; not re-reserving`);
    }
    return; // already pending / awaiting_onboarding / paid: idempotent
  }
  // One payout per task, even across re-recorded episodes.
  for (const p of ctx.db.payment.ticket_id.filter(ep.ticket_id)) {
    if (p.episode_id === 0n || p.episode_id === ep.id || !TDM11_RESERVED.includes(p.status)) continue;
    const other = ctx.db.episode.id.find(p.episode_id);
    if (other && other.task_id === ep.task_id) {
      throw new SenderError(`task ${ep.task_id} is already paid by payment ${p.id} (episode ${other.id})`);
    }
  }
  if (ep.contributor_id === 0n) {
    tdm11Log(ctx, actor, 'payment.skipped', {
      episode_id: ep.id,
      task_id: ep.task_id,
      ticket_id: ep.ticket_id,
      reason: 'episode has no contributor to pay',
    });
    return;
  }
  tdm11Reserve(ctx, actor, {
    ticket_id: ep.ticket_id,
    contributor_id: ep.contributor_id,
    episode_id: ep.id,
    dataset_offer_id: 0n,
    batch_ref: undefined,
    amount_cents: task.price_cents,
    data_plan_id: task.data_plan_id,
    existing,
    detail: { task_id: task.id, task_type: task.type },
  });
}

// ---------------------------------------------------------------- episodes

// Robot client / orchestrator records one episode. task_id and contributor_id
// are 0 for none; contributor_id 0 with a task defaults to the task's
// assignee. kind is baseline|eval|clean_teleop|recovery_takeover (purchased
// episodes come from ingest_purchased_episode). baseline/eval rollouts are
// 'not_reviewed' and stay out of the dataset; the others start 'pending' and
// increment dataset.total_count. checkpoint_uri is the policy that drove a
// rollout (none for teleop).
export const create_episode = spacetimedb.reducer(
  {
    ticket_id: t.u64(),
    task_id: t.u64(),
    contributor_id: t.u64(),
    kind: t.string(),
    storage_uri: t.string(),
    actions_uri: t.string(),
    cameras_json: t.string(),
    frame_count: t.u32(),
    duration_ms: t.u32(),
    takeover_ts: t.option(t.timestamp()),
    handback_ts: t.option(t.timestamp()),
    success: t.option(t.bool()),
    checkpoint_uri: t.option(t.string()),
  },
  (ctx, args) => {
    const role = requireRole(ctx, ...TDM11_EPISODE_WRITERS);
    const kind = oneOf('kind', args.kind, TDM11_CREATE_KINDS);
    const ticket = tdm11Ticket(ctx, args.ticket_id);
    if (TDM11_TERMINAL_TICKET.includes(ticket.status)) {
      throw new SenderError(`ticket ${ticket.id} is ${ticket.status}; no new episodes`);
    }
    if (args.storage_uri.trim() === '') throw new SenderError('storage_uri is required');
    const cameras_json = tdm11JsonText('cameras_json', args.cameras_json);
    if (
      args.takeover_ts && args.handback_ts &&
      args.handback_ts.microsSinceUnixEpoch < args.takeover_ts.microsSinceUnixEpoch
    ) {
      throw new SenderError('handback_ts is before takeover_ts');
    }

    let contributor_id = args.contributor_id;
    // TDM-125: an episode is labelled with the robot it was recorded on. A task's episode is
    // recorded on the task's robot (a split-robot ticket collects on another robot than it evals on).
    let robot_id = ticket.robot_id;
    if (args.task_id !== 0n) {
      const task = ctx.db.task.id.find(args.task_id);
      if (!task || task.ticket_id !== ticket.id) {
        throw new SenderError(`task ${args.task_id} not found on ticket ${ticket.id}`);
      }
      if (task.robot_id !== 0n) robot_id = task.robot_id;
      if (task.status === 'cancelled') throw new SenderError(`task ${task.id} is cancelled`);
      if (task.assignee_id === 0n) throw new SenderError(`task ${task.id} was never claimed`);
      if (contributor_id === 0n) contributor_id = task.assignee_id;
      if (contributor_id !== task.assignee_id) {
        throw new SenderError(
          `contributor ${contributor_id} is not task ${task.id}'s assignee (${task.assignee_id})`
        );
      }
      for (const other of ctx.db.episode.ticket_id.filter(ticket.id)) {
        if (other.task_id === task.id && (other.review_status === 'pending' || other.review_status === 'accepted')) {
          throw new SenderError(
            `task ${task.id} already has episode ${other.id} (${other.review_status}); one paid episode per task`
          );
        }
      }
    }
    if (contributor_id !== 0n && !ctx.db.contributor.id.find(contributor_id)) {
      throw new SenderError(`contributor ${contributor_id} not found`);
    }

    const inDataset = TDM11_DATASET_KINDS.includes(kind);
    const ep = ctx.db.episode.insert({
      id: 0n,
      ticket_id: ticket.id,
      robot_id,
      task_id: args.task_id,
      contributor_id,
      dataset_offer_id: 0n,
      kind,
      review_status: inDataset ? 'pending' : 'not_reviewed',
      checkpoint_uri: args.checkpoint_uri,
      storage_uri: args.storage_uri,
      cameras_json,
      actions_uri: args.actions_uri,
      frame_count: args.frame_count,
      duration_ms: args.duration_ms,
      takeover_ts: args.takeover_ts,
      handback_ts: args.handback_ts,
      success: args.success,
      created_at: ctx.timestamp,
    });
    let total_count: number | undefined;
    if (inDataset) {
      const ds = tdm11Dataset(ctx, ctx.db.ticket.id.find(ticket.id) ?? ticket);
      total_count = ds.total_count + 1;
      ctx.db.dataset.id.update({ ...ds, total_count, updated_at: ctx.timestamp });
    }
    tdm11Log(ctx, role === 'robot_client' ? `robot:${robot_id}` : role, 'episode.created', {
      episode_id: ep.id,
      ticket_id: ticket.id,
      robot_id,
      task_id: args.task_id,
      contributor_id,
      kind,
      review_status: ep.review_status,
      storage_uri: args.storage_uri,
      frame_count: args.frame_count,
      duration_ms: args.duration_ms,
      takeover_ts: args.takeover_ts?.microsSinceUnixEpoch,
      handback_ts: args.handback_ts?.microsSinceUnixEpoch,
      success: args.success,
      dataset_total_count: total_count,
    });
  }
);

// ---------------------------------------------------------------- reviews

// The reviewer's verdict. Final: a second review for the same episode is
// refused. Accept: episode -> accepted, dataset.accepted_count += 1, and if
// the episode came from a priced task a payment is reserved (pending, or
// blocked_budget over the cap). Reject: needs a reason; episode -> rejected;
// no payment. The reviewer column is the caller's service label.
export const submit_review = spacetimedb.reducer(
  { episode_id: t.u64(), accepted: t.bool(), reason: t.string(), metrics_json: t.string() },
  (ctx, { episode_id, accepted, reason, metrics_json }) => {
    const role = requireRole(ctx, ...TDM11_REVIEWERS);
    const ep = tdm11Episode(ctx, episode_id);
    const prior = [...ctx.db.review.episode_id.filter(episode_id)][0];
    if (prior) {
      throw new SenderError(
        `episode ${episode_id} already reviewed (review ${prior.id}, accepted=${prior.accepted}); reviews are final`
      );
    }
    if (ep.review_status !== 'pending') {
      throw new SenderError(`episode ${episode_id} is ${ep.review_status}; only pending episodes are reviewed`);
    }
    const why = reason.trim();
    if (!accepted && why === '') throw new SenderError('a rejected review needs a reason');
    if (why.length > TDM11_MAX_TEXT) throw new SenderError('reason too large');
    const metrics = tdm11JsonText('metrics_json', metrics_json);
    const reviewer = ctx.db.serviceIdentity.identity.find(ctx.sender)?.label ?? role;

    const rv = ctx.db.review.insert({
      id: 0n,
      episode_id,
      ticket_id: ep.ticket_id,
      accepted,
      reason: why,
      metrics_json: metrics,
      reviewer,
      created_at: ctx.timestamp,
    });
    ctx.db.episode.id.update({ ...ep, review_status: accepted ? 'accepted' : 'rejected' });

    let accepted_count: number | undefined;
    if (accepted && TDM11_DATASET_KINDS.includes(ep.kind)) {
      const ds = tdm11Dataset(ctx, tdm11Ticket(ctx, ep.ticket_id));
      accepted_count = ds.accepted_count + 1;
      ctx.db.dataset.id.update({ ...ds, accepted_count, updated_at: ctx.timestamp });
    }
    tdm11Log(ctx, 'reviewer', accepted ? 'review.accepted' : 'review.rejected', {
      review_id: rv.id,
      episode_id,
      ticket_id: ep.ticket_id,
      task_id: ep.task_id,
      contributor_id: ep.contributor_id,
      kind: ep.kind,
      reviewer,
      reason: why,
      metrics: JSON.parse(metrics),
      dataset_accepted_count: accepted_count,
    });
    if (accepted) tdm11ReserveEpisode(ctx, 'reviewer', ep);
  }
);

// ---------------------------------------------------------------- payments

// Retry / explicit form of the reservation submit_review already does: for an
// accepted episode, re-reserve a failed or blocked_budget payment (e.g. after
// approve_spend raised the cap). No-op if already pending/awaiting/paid.
export const reserve_payment = spacetimedb.reducer(
  { episode_id: t.u64() },
  (ctx, { episode_id }) => {
    const role = requireRole(ctx, ...TDM11_RESERVERS);
    tdm11ReserveEpisode(ctx, role, tdm11Episode(ctx, episode_id));
  }
);

// Payout for a completed task that produced no episode (a physical reset).
// Deduped on batch_ref "task:<id>"; refused if an episode exists for the task
// (that one is paid through its review).
export const reserve_task_payment = spacetimedb.reducer(
  { task_id: t.u64() },
  (ctx, { task_id }) => {
    const role = requireRole(ctx, ...TDM11_RESERVERS);
    const task = ctx.db.task.id.find(task_id);
    if (!task) throw new SenderError(`task ${task_id} not found`);
    if (task.status !== 'completed') throw new SenderError(`task ${task_id} is ${task.status}, not completed`);
    if (task.assignee_id === 0n) throw new SenderError(`task ${task_id} has no assignee to pay`);
    for (const ep of ctx.db.episode.ticket_id.filter(task.ticket_id)) {
      if (ep.task_id === task_id) {
        throw new SenderError(`task ${task_id} has episode ${ep.id}; it is paid through submit_review`);
      }
    }
    const ref = `task:${task_id}`;
    const existing = [...ctx.db.payment.ticket_id.filter(task.ticket_id)].find(
      (p) => p.episode_id === 0n && p.batch_ref === ref
    );
    if (existing && existing.status !== 'failed' && existing.status !== 'blocked_budget') {
      if (existing.status === 'reversed') throw new SenderError(`payment ${existing.id} was reversed`);
      return;
    }
    tdm11Reserve(ctx, role, {
      ticket_id: task.ticket_id,
      contributor_id: task.assignee_id,
      episode_id: 0n,
      dataset_offer_id: 0n,
      batch_ref: ref,
      amount_cents: task.price_cents,
      data_plan_id: task.data_plan_id,
      existing,
      detail: { task_id, task_type: task.type },
    });
  }
);

// Payout to a dataset seller for a settled offer (finalize_offer, TDM-14).
// Pays for accepted_count episodes minus those already reserved by earlier
// batches of the same offer; batch_ref names this batch (idempotent on it).
export const reserve_dataset_payment = spacetimedb.reducer(
  { dataset_offer_id: t.u64(), batch_ref: t.string() },
  (ctx, { dataset_offer_id, batch_ref }) => {
    const role = requireRole(ctx, ...TDM11_RESERVERS);
    const ref = batch_ref.trim();
    if (ref === '') throw new SenderError('batch_ref is required');
    const offer = ctx.db.datasetOffer.id.find(dataset_offer_id);
    if (!offer) throw new SenderError(`dataset_offer ${dataset_offer_id} not found`);
    if (offer.status !== 'done') {
      throw new SenderError(`dataset_offer ${dataset_offer_id} is ${offer.status}; finalize_offer first`);
    }
    if (offer.price_per_episode_cents === 0n) throw new SenderError('offer price is 0');
    const rows = [...ctx.db.payment.ticket_id.filter(offer.ticket_id)].filter(
      (p) => p.dataset_offer_id === dataset_offer_id
    );
    const existing = rows.find((p) => p.batch_ref === ref);
    if (existing && existing.status !== 'failed' && existing.status !== 'blocked_budget') {
      if (existing.status === 'reversed') throw new SenderError(`payment ${existing.id} was reversed`);
      return;
    }
    let paidEpisodes = 0n;
    for (const p of rows) {
      if (p !== existing && TDM11_RESERVED.includes(p.status)) {
        paidEpisodes += p.amount_cents / offer.price_per_episode_cents;
      }
    }
    const billable = BigInt(offer.accepted_count) - paidEpisodes;
    if (billable <= 0n) {
      throw new SenderError(
        `dataset_offer ${dataset_offer_id}: all ${offer.accepted_count} accepted episodes are already paid`
      );
    }
    tdm11Reserve(ctx, role, {
      ticket_id: offer.ticket_id,
      contributor_id: offer.seller_id,
      episode_id: 0n,
      dataset_offer_id,
      batch_ref: ref,
      amount_cents: billable * offer.price_per_episode_cents,
      data_plan_id: 0n,
      existing,
      detail: { billable_episodes: billable, price_per_episode_cents: offer.price_per_episode_cents },
    });
  }
);

// Payments service: the Stripe transfer succeeded. provider_ref is the tr_ id.
// Idempotent for the same provider_ref (webhook replays).
export const mark_payment_paid = spacetimedb.reducer(
  { payment_id: t.u64(), provider: t.string(), provider_ref: t.string() },
  (ctx, { payment_id, provider, provider_ref }) => {
    const role = requireRole(ctx, ...TDM11_PAYMENT_WRITERS);
    const p = tdm11Payment(ctx, payment_id);
    const ref = provider_ref.trim();
    if (ref === '') throw new SenderError('provider_ref is required');
    if (p.status === 'paid') {
      if (p.provider_ref === ref) return;
      throw new SenderError(`payment ${payment_id} already paid with ${p.provider_ref}`);
    }
    if (p.status !== 'pending' && p.status !== 'awaiting_onboarding') {
      throw new SenderError(`payment ${payment_id} is ${p.status}; only pending/awaiting_onboarding can be paid`);
    }
    ctx.db.payment.id.update({
      ...p,
      provider: provider.trim() || p.provider,
      provider_ref: ref,
      status: 'paid',
      error: undefined,
      updated_at: ctx.timestamp,
    });
    const c = ctx.db.contributor.id.find(p.contributor_id);
    if (c) ctx.db.contributor.id.update({ ...c, total_paid_cents: c.total_paid_cents + p.amount_cents });
    tdm11Log(ctx, role, 'payment.paid', {
      payment_id,
      ticket_id: p.ticket_id,
      contributor_id: p.contributor_id,
      episode_id: p.episode_id,
      dataset_offer_id: p.dataset_offer_id,
      batch_ref: p.batch_ref,
      amount_cents: p.amount_cents,
      provider: provider.trim() || p.provider,
      provider_ref: ref,
    });
  }
);

// Payments service: the transfer failed. Releases the reserved spend; a later
// reserve_payment / reserve_*_payment re-reserves it.
export const mark_payment_failed = spacetimedb.reducer(
  { payment_id: t.u64(), error: t.string() },
  (ctx, { payment_id, error }) => {
    const role = requireRole(ctx, ...TDM11_PAYMENT_WRITERS);
    const p = tdm11Payment(ctx, payment_id);
    if (p.status === 'failed') return;
    if (p.status !== 'pending' && p.status !== 'awaiting_onboarding') {
      throw new SenderError(`payment ${payment_id} is ${p.status}; only pending/awaiting_onboarding can fail`);
    }
    const msg = error.trim() || 'unspecified';
    ctx.db.payment.id.update({ ...p, status: 'failed', error: msg, updated_at: ctx.timestamp });
    tdm11Release(ctx, p.ticket_id, p.amount_cents);
    tdm11Log(ctx, role, 'payment.failed', {
      payment_id,
      ticket_id: p.ticket_id,
      contributor_id: p.contributor_id,
      episode_id: p.episode_id,
      amount_cents: p.amount_cents,
      error: msg,
      released_cents: p.amount_cents,
    });
  }
);

// Payments service: the contributor's Stripe account cannot receive transfers
// yet. Budget stays reserved; mark_payment_paid works once they onboard.
export const mark_payment_awaiting_onboarding = spacetimedb.reducer(
  { payment_id: t.u64(), note: t.string() },
  (ctx, { payment_id, note }) => {
    const role = requireRole(ctx, ...TDM11_PAYMENT_WRITERS);
    const p = tdm11Payment(ctx, payment_id);
    if (p.status === 'awaiting_onboarding') return;
    if (p.status !== 'pending') throw new SenderError(`payment ${payment_id} is ${p.status}, not pending`);
    ctx.db.payment.id.update({
      ...p,
      status: 'awaiting_onboarding',
      error: note.trim() || undefined,
      updated_at: ctx.timestamp,
    });
    tdm11Log(ctx, role, 'payment.awaiting_onboarding', {
      payment_id,
      contributor_id: p.contributor_id,
      amount_cents: p.amount_cents,
      note,
    });
  }
);

// Payments service: a paid transfer was reversed (reverse_payment or the
// transfer.reversed webhook). Releases the spend and the contributor total.
export const mark_payment_reversed = spacetimedb.reducer(
  { payment_id: t.u64(), reason: t.string() },
  (ctx, { payment_id, reason }) => {
    const role = requireRole(ctx, ...TDM11_PAYMENT_WRITERS);
    const p = tdm11Payment(ctx, payment_id);
    if (p.status === 'reversed') return;
    if (p.status !== 'paid') throw new SenderError(`payment ${payment_id} is ${p.status}, not paid`);
    const why = reason.trim() || 'reversed';
    ctx.db.payment.id.update({ ...p, status: 'reversed', error: why, updated_at: ctx.timestamp });
    tdm11Release(ctx, p.ticket_id, p.amount_cents);
    const c = ctx.db.contributor.id.find(p.contributor_id);
    if (c) {
      const total = c.total_paid_cents > p.amount_cents ? c.total_paid_cents - p.amount_cents : 0n;
      ctx.db.contributor.id.update({ ...c, total_paid_cents: total });
    }
    tdm11Log(ctx, role, 'payment.reversed', {
      payment_id,
      ticket_id: p.ticket_id,
      contributor_id: p.contributor_id,
      amount_cents: p.amount_cents,
      provider_ref: p.provider_ref,
      reason: why,
    });
  }
);

// ---------------------------------------------------------------- spend approval

// Dashboard "Approve spend" button and the MCP connector's approve_spend.
// Sets data_plan.approved_cents (0 = the plan's est_cost_cents) and status
// approved, and recomputes ticket.approved_cents as the sum over its plans.
// Refused if the ticket's total approved spend would exceed budget_cents, or
// if it would drop below what this plan's tasks already reserved.
// approved_by is the human, e.g. "human:web" or "human:via_mcp".
export const approve_spend = spacetimedb.reducer(
  { ticket_id: t.u64(), data_plan_id: t.u64(), approved_cents: t.u64(), approved_by: t.string() },
  (ctx, { ticket_id, data_plan_id, approved_cents, approved_by }) => {
    const role = requireRole(ctx, ...TDM11_SPEND_APPROVERS);
    const ticket = tdm11Ticket(ctx, ticket_id);
    if (TDM11_TERMINAL_TICKET.includes(ticket.status)) {
      throw new SenderError(`ticket ${ticket_id} is ${ticket.status}; spend is frozen`);
    }
    const plan = ctx.db.dataPlan.id.find(data_plan_id);
    if (!plan || plan.ticket_id !== ticket_id) {
      throw new SenderError(`data_plan ${data_plan_id} not found on ticket ${ticket_id}`);
    }
    if (plan.status === 'rejected' || plan.status === 'done') {
      throw new SenderError(`data_plan ${data_plan_id} is ${plan.status}; cannot approve spend`);
    }
    const who = approved_by.trim();
    if (who === '') throw new SenderError('approved_by is required');
    const amount = approved_cents === 0n ? plan.est_cost_cents : approved_cents;
    if (amount === 0n) throw new SenderError('approved amount is 0');

    let otherApproved = 0n;
    for (const other of ctx.db.dataPlan.ticket_id.filter(ticket_id)) {
      if (other.id !== data_plan_id && other.status !== 'rejected') otherApproved += other.approved_cents;
    }
    const total = otherApproved + amount;
    if (total > ticket.budget_cents) {
      throw new SenderError(
        `approving ${amount} brings ticket ${ticket_id} approved spend to ${total} ` +
          `> budget_cents ${ticket.budget_cents} (remaining ${ticket.budget_cents - otherApproved})`
      );
    }
    const reserved = tdm11PlanReserved(ctx, ticket_id, data_plan_id, 0n);
    if (amount < reserved) {
      throw new SenderError(`data_plan ${data_plan_id} already has ${reserved} reserved; cannot approve less`);
    }
    ctx.db.dataPlan.id.update({
      ...plan,
      status: plan.status === 'proposed' ? 'approved' : plan.status,
      approved_cents: amount,
      approved_by: who,
      approved_at: ctx.timestamp,
    });
    ctx.db.ticket.id.update({ ...ticket, approved_cents: total, updated_at: ctx.timestamp });
    tdm11Log(ctx, who.startsWith('human:') ? who : role, 'spend.approved', {
      ticket_id,
      data_plan_id,
      data_type: plan.data_type,
      amount: plan.amount,
      est_cost_cents: plan.est_cost_cents,
      approved_cents: amount,
      previous_approved_cents: plan.approved_cents,
      ticket_approved_cents: total,
      budget_cents: ticket.budget_cents,
      spent_cents: ticket.spent_cents,
      approved_by: who,
      via: role,
    });
  }
);
