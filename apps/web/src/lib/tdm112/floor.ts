// TDM-112: the factory floor's view model. Pure: no React, no imports with side effects, so
// `pnpm --filter @rdf/web test` (test/tdm112-floor.test.ts) runs it under plain Node.
//
// One ticket's rows (ticket, eval_run, eval_trial, failure_cluster, data_plan, task, episode, review,
// training_run, agent_session) plus its event_log rows -> eight agent stations on an assembly line,
// each with a state, one line of current activity in plain words, the LLM behind it and a timer, and
// the Factory Droids standing at (or asleep on) the station they serve. Nothing is invented: a station
// with no rows says what it waits for.


// ------------------------------------------------------------------------------ stations

export type Tdm112StationKey = 'baseline' | 'diagnose' | 'plan_data' | 'collect' | 'review' | 'train' | 'eval' | 'ship';
/** idle steel, working melt (pulsing rivet), waiting on a human ochre, done ink, failed slag. */
export type Tdm112State = 'idle' | 'working' | 'waiting' | 'done' | 'failed';

export interface Tdm112StationDef {
  key: Tdm112StationKey;
  label: string;
  /** Second word for the two Eval stations. */
  sub?: string;
  agent: string;
  /** ticket.status values this station runs. */
  statuses: readonly string[];
  /** llm.call `step` of the Claude agent at this station. */
  llmStep?: string;
  waitsFor: string;
}

export const TDM112_STATIONS: readonly Tdm112StationDef[] = [
  { key: 'baseline', label: 'Eval', sub: 'Baseline', agent: 'Eval runner', statuses: ['baseline'], waitsFor: 'Waits for the robot to be onboarded.' },
  { key: 'diagnose', label: 'Diagnose', agent: 'Failure clustering, Claude', statuses: ['diagnose'], llmStep: 'diagnose', waitsFor: 'Waits for a finished eval with failed trials.' },
  { key: 'plan_data', label: 'Plan data', agent: 'Planner, Claude', statuses: ['plan_data'], llmStep: 'planner', waitsFor: 'Waits for the failure clusters.' },
  { key: 'collect', label: 'Collect', agent: 'Dispatcher and contributors', statuses: ['dispatch'], waitsFor: 'Waits for an approved data plan.' },
  { key: 'review', label: 'Review', agent: 'Reviewer and dataset QA', statuses: ['review'], waitsFor: 'Waits for recorded episodes.' },
  { key: 'train', label: 'Train', agent: 'Training Droid and Modal', statuses: ['train'], waitsFor: 'Waits for enough accepted episodes.' },
  { key: 'eval', label: 'Eval', sub: 'Fine-tuned', agent: 'Eval runner', statuses: ['eval'], waitsFor: 'Waits for a fine-tuned checkpoint.' },
  { key: 'ship', label: 'Ship', agent: 'Flywheel', statuses: ['shipped'], waitsFor: 'Waits for an eval that meets the target.' },
];

export function tdm112StationIndex(status: string): number {
  return TDM112_STATIONS.findIndex((s) => s.statuses.includes(status));
}

// ------------------------------------------------------------------------------ inputs (structural)

export type TsLike = { microsSinceUnixEpoch: bigint };
type OptTs = TsLike | null | undefined;
const us = (t: OptTs): number => (t ? Number(t.microsSinceUnixEpoch) : 0);

export interface Tdm112Ticket {
  id: bigint;
  status: string;
  iteration: number;
  targetSuccess: number;
  createdAt: TsLike;
  deployedCheckpointUri?: string | null;
}
export interface Tdm112EvalRun {
  id: bigint;
  kind: string;
  status: string;
  trials: number;
  completed: number;
  successes: number;
  trainingRunId: bigint;
  createdAt: TsLike;
  finishedAt?: OptTs;
}
export interface Tdm112EvalTrial { evalRunId: bigint; trialIndex: number; success: boolean; failureNote?: string | null }
export interface Tdm112Cluster { iteration: number; label: string; count: number }
export interface Tdm112Plan {
  id: bigint;
  iteration: number;
  dataType: string;
  amount: number;
  estCostCents: bigint;
  approvedCents: bigint;
  status: string;
  createdAt: TsLike;
}
export interface Tdm112Task { id: bigint; type: string; status: string; dataPlanId: bigint; priceCents: bigint }
export interface Tdm112Episode { id: bigint; kind: string; reviewStatus: string }
export interface Tdm112Review { episodeId: bigint; accepted: boolean }
export interface Tdm112TrainingRun {
  id: bigint;
  iteration: number;
  status: string;
  modalCallId?: string | null;
  hyperparamsJson: string;
  metricsJson: string;
  checkpointUri?: string | null;
  error?: string | null;
  createdAt: TsLike;
  startedAt?: OptTs;
  finishedAt?: OptTs;
}
export interface Tdm112Session {
  id: bigint;
  ticketId: bigint;
  role: string;
  name: string;
  status: string;
  currentStep: string;
  waitingForJson?: string | null;
  createdAt: TsLike;
  lastUpdate: TsLike;
}
/** One event_log row, payload parsed. */
export interface Tdm112Event { id: number; tsUs: number; actor: string; type: string; payload: Record<string, unknown> }

export interface Tdm112Input {
  ticket: Tdm112Ticket;
  maxIterations?: number;
  evalRuns?: readonly Tdm112EvalRun[];
  evalTrials?: readonly Tdm112EvalTrial[];
  clusters?: readonly Tdm112Cluster[];
  plans?: readonly Tdm112Plan[];
  tasks?: readonly Tdm112Task[];
  episodes?: readonly Tdm112Episode[];
  reviews?: readonly Tdm112Review[];
  trainingRuns?: readonly Tdm112TrainingRun[];
  /** This ticket's event_log rows, oldest first. */
  events?: readonly Tdm112Event[];
  /** llm.call rows (they carry no ticket id), oldest first. */
  llmCalls?: readonly Tdm112Event[];
  /** agent.resumed rows (matched to droids by session_id), oldest first. */
  resumes?: readonly Tdm112Event[];
  /** Droid sessions of this ticket. */
  sessions?: readonly Tdm112Session[];
  /** WAITING_FOR json -> plain words (the component passes TDM-76's predicateWords). */
  describePredicate?: (json: string) => string;
  nowUs: number;
}

// ------------------------------------------------------------------------------ outputs

export interface Tdm112Chip { index: number; pass: boolean }

export interface Tdm112StationView {
  key: Tdm112StationKey;
  index: number;
  label: string;
  sub?: string;
  agent: string;
  state: Tdm112State;
  /** One line, plain words: the newest relevant event_log row, else what the station waits for. */
  activity: string;
  activityTsUs?: number;
  activityIsError: boolean;
  /** Short numeric facts ("4/6 trials", "3 clusters"). */
  facts: string[];
  chips?: Tdm112Chip[];
  chipsTotal?: number;
  /** For LLM agents: backend and model from the newest llm.call of this step. */
  llm?: { backend: string; model: string | null };
  /** STUB vs REAL for the train station; SEEDED for an eval whose outcomes were drawn, not run. */
  computeTag?: 'STUB' | 'REAL' | 'SEEDED';
  startedUs?: number;
  endedUs?: number;
  /** Times the station ran (loop-backs run it again). */
  runs: number;
  waitsFor: string;
  /** The events that belong to this station, newest last (the drawer shows the tail). */
  eventIds: number[];
}

export type Tdm112DroidState = 'working' | 'asleep' | 'waking' | 'done' | 'failed' | 'docked';
export interface Tdm112DroidView {
  id: number;
  ticketId: number;
  role: string;
  label: string;
  name: string;
  /** Station it stands at; -1 = the droid bay at the head of the line (not acting). */
  stationIndex: number;
  homeIndex: number;
  state: Tdm112DroidState;
  step: string;
  /** WAITING_FOR predicate (raw form when the droid wrote one) and its plain-words reading. */
  predicate?: { raw?: string; words: string };
  /** The last SpacetimeDB wake-up: 'woke on <table>:<field>'. */
  woke?: { tsUs: number; on: string; recent: boolean };
}

export interface Tdm112Floor {
  ticketId: number;
  stations: Tdm112StationView[];
  /** The station running now (or waiting on a human); -1 when none (queued, ended). */
  activeIndex: number;
  iteration: number;
  maxIterations: number;
  /** "Iteration 2 of 3" once the loop has gone round at least once. */
  iterationLabel?: string;
  ended?: 'shipped' | 'failed' | 'cancelled';
  /** The note the flywheel wrote when the ticket failed or was cancelled. */
  endedNote?: string;
  queued: boolean;
  droids: Tdm112DroidView[];
}

// ------------------------------------------------------------------------------ helpers

const s = (v: unknown): string => (v === undefined || v === null ? '' : String(v));
const n = (v: unknown): number | undefined => {
  const x = typeof v === 'number' ? v : typeof v === 'string' && v.trim() !== '' ? Number(v) : typeof v === 'bigint' ? Number(v) : NaN;
  return Number.isFinite(x) ? x : undefined;
};
const usd = (cents: unknown): string => `$${((n(cents) ?? 0) / 100).toFixed(2)}`;
const plural = (k: number, one: string, many = `${one}s`) => `${k} ${k === 1 ? one : many}`;

function parseObj(text: string | null | undefined): Record<string, unknown> {
  if (!text) return {};
  try {
    const v = JSON.parse(text);
    return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

/** Strip harness tags, collapse whitespace, capitalise. */
function tidy(text: string): string {
  const t = text.replace(/^\s*\[(SIM|FAKE|SEEDED)\]\s*/i, '').replace(/\s+/g, ' ').trim();
  return t ? t[0].toUpperCase() + t.slice(1) : t;
}

export function tdm112Clip(text: string, max = 120): string {
  const cps = Array.from(text);
  return cps.length <= max ? text : `${cps.slice(0, max - 1).join('').replace(/\s+\S*$/, '')}…`;
}

const DATA_UNIT: Record<string, [string, string]> = {
  recovery_takeover: ['recovery takeover', 'recovery takeovers'],
  clean_teleop: ['clean demo', 'clean demos'],
  purchased: ['purchased episode', 'purchased episodes'],
  reset: ['reset', 'resets'],
};
const unitOf = (type: string, k: number) => (DATA_UNIT[type] ? DATA_UNIT[type][k === 1 ? 0 : 1] : type.replace(/_/g, ' '));

const STATUS_WORD: Record<string, string> = {
  baseline: 'Baseline eval', diagnose: 'Diagnose', plan_data: 'Plan data', dispatch: 'Collect', review: 'Review',
  train: 'Train', eval: 'Eval', shipped: 'Ship', failed: 'Failed', cancelled: 'Cancelled', created: 'Queued', onboarding: 'Onboarding',
};

const DROID_HOME: Record<string, number> = { onboarding_droid: 0, data_droid: 3, training_droid: 5 };
const DROID_LABEL: Record<string, string> = { onboarding_droid: 'Onboarding Droid', data_droid: 'Data Droid', training_droid: 'Training Droid' };
export const tdm112IsDroidRole = (role: string) => role.endsWith('_droid');

// ------------------------------------------------------------------------------ event -> station

const EVALISH = ['eval.', 'rollout.', 'sim.scene_reset', 'robot.waiting_reset', 'robot.reset_done'];
const starts = (t: string, ps: readonly string[]) => ps.some((p) => t.startsWith(p));

/**
 * Which station an event belongs to, or null (robot telemetry, payments, infra). Eval events go to the
 * baseline or the fine-tuned eval by their `kind` / eval run, else by the ticket status at that moment.
 */
export function tdm112StationOfEvent(
  e: Pick<Tdm112Event, 'type' | 'payload' | 'tsUs'>,
  ctx: {
    statusAt: (tsUs: number) => string;
    runKind: (id: number) => string | undefined;
    taskType: (id: number) => string | undefined;
    /** The last of baseline / eval the ticket was in at that moment. */
    lastEvalStatusAt?: (tsUs: number) => string | undefined;
  },
): Tdm112StationKey | null {
  const t = e.type;
  const p = e.payload;
  const evalSide = (): Tdm112StationKey => {
    const kind = s(p.kind) || ctx.runKind(n(p.eval_run_id) ?? -1) || '';
    if (kind === 'baseline') return 'baseline';
    if (kind === 'candidate' || kind === 'ab_live' || kind === 'eval') return 'eval';
    const st = ctx.statusAt(e.tsUs);
    // Rollouts outside an eval are collection (DAgger rollouts that contributors take over).
    if (kind || (st !== 'baseline' && st !== 'eval')) return st === 'baseline' ? 'baseline' : st === 'eval' ? 'eval' : 'collect';
    return st === 'eval' ? 'eval' : 'baseline';
  };
  if (t === 'flywheel.transition') return null; // handoffs, shown as the part on the conveyor
  if (starts(t, EVALISH)) return evalSide();
  if (t.startsWith('diagnose.') || t === 'planner.failure_cluster') return 'diagnose';
  if (t.startsWith('planner.') || t === 'plan.decision' || t === 'spend.approved' || t.startsWith('plan_offer.') || t.startsWith('flywheel.auto_approve')) return 'plan_data';
  if (t.startsWith('review.') || t.startsWith('qa.')) return 'review';
  if (t.startsWith('training.') || t.startsWith('modal.')) return 'train';
  if (t === 'flywheel.shipped' || t === 'flywheel.ship_blocked' || t.startsWith('ticket.checkpoint_deployed') || t === 'flywheel.report_failed') return 'ship';
  if (t === 'episode.created') {
    const k = s(p.kind);
    if (k === 'baseline') return 'baseline';
    if (k === 'eval') return evalSide();
    return 'collect';
  }
  if (t.startsWith('task.') || t.startsWith('dispatch.') || t.startsWith('job_drop.') || t.startsWith('intervention.') || t.startsWith('teleop.')) {
    // Resets (and the paging for them) during an eval belong to the eval station that asked for them;
    // only data tasks (demos, takeovers) are collection.
    const tt = s(p.type) || s(p.task_type) || ctx.taskType(n(p.task_id) ?? -1) || '';
    const st = ctx.statusAt(e.tsUs);
    const dataTask = tt === 'clean_teleop' || tt === 'recovery_takeover' || tt === 'purchased';
    if (!dataTask && (st === 'baseline' || st === 'eval')) return st === 'eval' ? 'eval' : 'baseline';
    // The reset after an eval's last trial lands just after the ticket left the eval.
    // (An eval is followed by diagnose, or by ship; resets during collection stay collection.)
    if ((tt === 'reset' || t.startsWith('dispatch.reset')) && (st === 'diagnose' || st === 'shipped' || st === 'failed'))
      return ctx.lastEvalStatusAt?.(e.tsUs) === 'eval' ? 'eval' : 'baseline';
    return 'collect';
  }
  return null;
}

// ------------------------------------------------------------------------------ event -> words

/** One event_log row in plain words. Model text is never paraphrased: reasons are quoted as written. */
export function tdm112Words(e: Pick<Tdm112Event, 'type' | 'payload'>): string {
  const p = e.payload;
  const idx = n(p.index);
  const trial = idx !== undefined ? idx + 1 : undefined;
  const of = n(p.trials);
  const reason = s(p.reason);
  const note = s(p.note) || s(p.failure_note);
  switch (e.type) {
    case 'eval.policy_activated':
      return `Loaded the ${s(p.kind) === 'baseline' ? 'base' : 'fine-tuned'} policy`;
    case 'eval.started':
      return `Started ${s(p.kind) === 'baseline' ? 'the baseline' : 'an eval'}: ${plural(of ?? 0, 'trial')}`;
    case 'eval.dispatched':
      return `Sent ${plural(of ?? 0, 'trial')} to the robot`;
    case 'eval.trials_started':
      return `Robot started ${plural(of ?? 0, 'trial')}`;
    case 'eval.trial_started':
      return `Trial ${trial ?? '?'}${of ? ` of ${of}` : ''} running`;
    case 'eval.trial':
    case 'eval.trial_recorded':
      return `Trial ${trial ?? '?'}: ${p.success ? 'pass' : 'fail'}${note ? `, ${tidy(note).replace(/^Auto outcome from sim state: /i, '').toLowerCase()}` : ''}`;
    case 'eval.finalized': {
      const k = n(p.successes);
      const t = n(p.trials) ?? n(p.completed);
      return k !== undefined && t !== undefined ? `Finished: ${k}/${t} passed` : 'Eval finished';
    }
    case 'eval.solo_reset':
      return 'Scene reset by the simulator (unattended)';
    case 'eval.aborted':
      return `Eval stopped${reason ? `: ${reason}` : ''}`;
    case 'rollout.started':
      return 'Policy rollout running';
    case 'rollout.ended':
      return `Rollout ended after ${n(p.steps) ?? '?'} steps${p.success === true ? ', cube placed' : ''}`;
    case 'robot.waiting_reset':
      return tidy(reason || 'Waiting for a scene reset');
    case 'robot.reset_done':
      return 'Scene reset done';
    case 'sim.scene_reset':
      return tidy(note || 'Simulator reset the scene');
    case 'diagnose.start':
    case 'diagnose.started':
      return `Clustering the failed trials${n(p.failed) !== undefined ? ` (${n(p.failed)})` : ''}`;
    case 'diagnose.clusters': {
      const cl = Array.isArray(p.clusters) ? p.clusters.length : n(p.count) ?? 0;
      return `Found ${plural(cl, 'failure cluster')}`;
    }
    case 'diagnose.no_failures':
      return 'No failed trials to cluster';
    case 'diagnose.done':
      return 'Diagnosis written';
    case 'planner.failure_cluster':
      return `Cluster "${s(p.label)}": ${plural(n(p.count) ?? 0, 'trial')}`;
    case 'planner.data_plan':
      return `Plan: buy ${n(p.amount) ?? '?'} ${unitOf(s(p.data_type), n(p.amount) ?? 0)}, est. ${usd(p.est_cost_cents)}`;
    case 'plan.decision':
      return s(p.decision) ? tidy(s(p.decision)) : 'Decision logged';
    case 'spend.approved':
      return `Spend approved: ${usd(p.approved_cents)}${s(p.approved_by) ? ` by ${s(p.approved_by)}` : ''}`;
    case 'flywheel.auto_approve':
      return `Auto-approved ${usd(p.approved_cents ?? p.cents)}`;
    case 'dispatch.plan_dispatched':
    case 'dispatch.plan':
      return `Posted ${plural(n(p.created) ?? n(p.count) ?? n(p.tasks) ?? (Array.isArray(p.task_ids) ? p.task_ids.length : 0), 'paid task')}`;
    case 'dispatch.reset_requested':
      return `Asked for a scene reset (${usd(p.price_cents)})`;
    case 'dispatch.paged':
      return 'Paged contributors';
    case 'dispatch.target_reached':
      return 'Collection target reached';
    case 'task.created':
      return `Posted ${unitOf(s(p.type), 1)} task #${s(p.task_id)} at ${usd(p.price_cents)}`;
    case 'task.claimed':
      return `Task #${s(p.task_id)} claimed by a contributor`;
    case 'task.completed':
      return `Task #${s(p.task_id)} done`;
    case 'task.cancelled':
      return `Task #${s(p.task_id)} cancelled${reason ? `: ${reason}` : ''}`;
    case 'task.released':
      return `Task #${s(p.task_id)} released`;
    case 'task.lease_expired':
      return `Task #${s(p.task_id)} lease expired`;
    case 'job_drop.posted':
      return `Posted task #${s(p.task_id)} to ${s(p.channel) || 'a channel'}`;
    case 'job_drop.preview':
      return `Drafted a job drop for task #${s(p.task_id)} (dry run)`;
    case 'job_drop.claimed':
      return `Claim from a ${s(p.src) || 'job'} drop`;
    case 'episode.created':
      return `Episode #${s(p.episode_id)} recorded (${unitOf(s(p.kind), 1)})`;
    case 'review.start':
      return `Reviewing episode #${s(p.episode_id)}`;
    case 'review.accepted':
      return `Episode #${s(p.episode_id)} accepted`;
    case 'review.rejected':
      return `Episode #${s(p.episode_id)} rejected${reason ? `: ${reason}` : ''}`;
    case 'training.queued':
      return 'Training run queued';
    case 'training.launch':
    case 'training.convert_spawned':
      return 'Converting episodes to a LeRobot dataset on Modal';
    case 'training.finetune_spawned':
      return 'Fine-tune running on Modal';
    case 'training.stub_running':
      return 'Stub trainer running, no GPU';
    case 'training.status':
      return `Training ${s(p.to) || s(p.status)}`;
    case 'training.done':
      return 'Training done, checkpoint written';
    case 'training.failed':
      return `Training failed${s(p.error) ? `: ${s(p.error)}` : ''}`;
    case 'flywheel.shipped':
      return 'Shipped the checkpoint';
    case 'flywheel.ship_blocked':
      return `Ship blocked${reason ? `: ${reason}` : ''}`;
    case 'ticket.checkpoint_deployed':
      return 'Checkpoint deployed to the robot';
    default: {
      const [area, ...rest] = e.type.split('.');
      const what = rest.join(' ').replace(/_/g, ' ');
      const head = `${area[0]?.toUpperCase() ?? ''}${area.slice(1).replace(/_/g, ' ')}: ${what}`;
      const why = s(p.error) || reason;
      return why ? `${head}, ${why}` : head;
    }
  }
}

const isErrorType = (t: string) => {
  const tail = t.split('.').pop() ?? '';
  return tail === 'error' || tail === 'failed' || tail === 'aborted' || tail === 'blocked' || tail.endsWith('_failed') || tail.endsWith('_error');
};

function firstLeaf(o: unknown, depth = 0): { table: string; field?: string } | null {
  if (!o || typeof o !== 'object' || depth > 8) return null;
  const obj = o as Record<string, unknown>;
  for (const k of ['all', 'any']) {
    if (Array.isArray(obj[k])) {
      for (const c of obj[k] as unknown[]) {
        const l = firstLeaf(c, depth + 1);
        if (l) return l;
      }
      return null;
    }
  }
  if (typeof obj.table !== 'string') return null;
  return { table: obj.table, field: typeof obj.field === 'string' && obj.field ? obj.field : typeof obj.agg === 'string' ? obj.agg : undefined };
}

/** "woke on episode:accepted_count" from an agent.resumed row the resume bridge wrote. */
export function tdm112WokeOn(e: Pick<Tdm112Event, 'type' | 'payload'>): string | null {
  if (e.type !== 'agent.resumed') return null;
  const reason = s(e.payload.reason);
  if (!reason.startsWith('predicate true:')) return null;
  const leaf = firstLeaf(e.payload.satisfied_predicate);
  if (leaf) return `${leaf.table}:${leaf.field ?? 'row'}`;
  const pred = reason.slice('predicate true:'.length).split(';')[0].trim();
  const m = /^([a-z_]+)[.:]([a-z_]+)/.exec(pred);
  return m ? `${m[1]}:${m[2]}` : pred || null;
}

// ------------------------------------------------------------------------------ the floor

export function tdm112Floor(input: Tdm112Input): Tdm112Floor {
  const { ticket, nowUs } = input;
  const evalRuns = input.evalRuns ?? [];
  const trials = input.evalTrials ?? [];
  const clusters = input.clusters ?? [];
  const plans = input.plans ?? [];
  const tasks = input.tasks ?? [];
  const episodes = input.episodes ?? [];
  const reviews = input.reviews ?? [];
  const training = input.trainingRuns ?? [];
  const events = input.events ?? [];
  const maxIterations = Math.max(1, input.maxIterations ?? 3);
  const iteration = ticket.iteration;

  // Status history: when each station was entered and left.
  const changes = events
    .filter((e) => e.type === 'ticket.status_changed')
    .map((e) => ({ tsUs: e.tsUs, from: s(e.payload.from), to: s(e.payload.to) }));
  const statusAt = (tsUs: number): string => {
    let st = 'created';
    for (const c of changes) if (c.tsUs <= tsUs) st = c.to;
    return st;
  };
  const lastEvalStatusAt = (tsUs: number): string | undefined => {
    let st: string | undefined;
    for (const c of changes) if (c.tsUs <= tsUs && (c.to === 'baseline' || c.to === 'eval')) st = c.to;
    return st;
  };
  const runKind = (id: number) => evalRuns.find((r) => Number(r.id) === id)?.kind;
  const taskType = (id: number) => tasks.find((t) => Number(t.id) === id)?.type;

  const status = ticket.status;
  const ended = status === 'shipped' || status === 'failed' || status === 'cancelled' ? (status as Tdm112Floor['ended']) : undefined;
  let stoppedAt = -1;
  let endedNote: string | undefined;
  if (status === 'failed' || status === 'cancelled') {
    const last = [...events].reverse().find((e) => e.type === 'ticket.status_changed' && s(e.payload.to) === status);
    stoppedAt = tdm112StationIndex(s(last?.payload.from));
    endedNote = s(last?.payload.note) || undefined;
  }
  const cur = ended ? (status === 'shipped' ? TDM112_STATIONS.length - 1 : stoppedAt) : tdm112StationIndex(status);

  // Group events by station.
  const byStation = new Map<Tdm112StationKey, Tdm112Event[]>();
  for (const e of events) {
    const k = tdm112StationOfEvent(e, { statusAt, runKind, taskType, lastEvalStatusAt });
    if (!k) continue;
    const arr = byStation.get(k) ?? [];
    arr.push(e);
    byStation.set(k, arr);
  }

  const newest = <T extends { createdAt: TsLike }>(rows: readonly T[]) => [...rows].sort((a, b) => us(b.createdAt) - us(a.createdAt))[0];
  const baselineRun = newest(evalRuns.filter((r) => r.kind === 'baseline'));
  const candRun = newest(evalRuns.filter((r) => r.kind !== 'baseline'));
  const chipsOf = (run: Tdm112EvalRun | undefined) =>
    run
      ? trials
          .filter((t) => t.evalRunId === run.id)
          .sort((a, b) => a.trialIndex - b.trialIndex)
          .map((t) => ({ index: t.trialIndex, pass: t.success }))
      : undefined;
  const evalFacts = (run: Tdm112EvalRun | undefined): string[] => {
    if (!run) return [];
    if (run.status === 'done') return [`${run.successes}/${run.trials} passed`];
    if (run.status === 'running' || run.status === 'queued')
      return [`trial ${Math.min(run.completed + 1, run.trials)}/${run.trials}`, ...(run.completed ? [`${run.successes}/${run.completed} pass`] : [])];
    return [`${run.successes}/${run.completed} of ${run.trials}`, run.status];
  };

  const itClusters = clusters.filter((c) => c.iteration === iteration);
  const shownClusters = itClusters.length ? itClusters : clusters.filter((c) => c.iteration === Math.max(-1, ...clusters.map((x) => x.iteration)));
  const plan = newest(plans.filter((p) => p.iteration === iteration)) ?? newest(plans);
  const dataTasks = tasks.filter((t) => t.type !== 'reset' && (!plan || t.dataPlanId === plan.id || t.dataPlanId === 0n));
  const openTasks = dataTasks.filter((t) => t.status === 'open').length;
  const claimedTasks = dataTasks.filter((t) => t.status === 'claimed' || t.status === 'submitted').length;
  const doneTasks = dataTasks.filter((t) => t.status === 'completed').length;
  const openResets = tasks.filter((t) => t.type === 'reset' && t.status === 'open').length;
  const dataEpisodes = episodes.filter((e) => e.kind !== 'baseline' && e.kind !== 'eval');
  const dataIds = new Set(dataEpisodes.map((e) => e.id));
  const dataReviews = reviews.filter((r) => dataIds.has(r.episodeId));
  const accepted = dataReviews.filter((r) => r.accepted).length;
  const rejected = dataReviews.length - accepted;
  const pendingReview = dataEpisodes.filter((e) => e.reviewStatus === 'pending' || e.reviewStatus === 'not_reviewed').length;
  const run = newest(training);
  // Job drops (Discord and the other channels) for this ticket's data tasks; reset drops are the eval's.
  const dataTaskIds = new Set(tasks.filter((t) => t.type !== 'reset').map((t) => Number(t.id)));
  const forDataTask = (e: Tdm112Event) => {
    const ids = Array.isArray(e.payload.task_ids) ? (e.payload.task_ids as unknown[]) : [e.payload.task_id];
    return ids.some((x) => dataTaskIds.has(n(x) ?? -1));
  };
  const drops = events.filter((e) => e.type === 'job_drop.posted' && forDataTask(e)).length;
  const drafted = events.filter((e) => e.type === 'job_drop.preview' && forDataTask(e)).length;

  const llmFor = (step: string | undefined, fromUs: number) => {
    if (!step) return undefined;
    const calls = (input.llmCalls ?? []).filter((c) => s(c.payload.step) === step);
    const inWindow = calls.filter((c) => c.tsUs >= fromUs);
    const c = (inWindow.length ? inWindow : calls)[(inWindow.length ? inWindow : calls).length - 1];
    if (!c) return undefined;
    const model = s(c.payload.served_model) || s(c.payload.model) || null;
    return { backend: s(c.payload.backend) || 'n/a', model };
  };

  const stations: Tdm112StationView[] = TDM112_STATIONS.map((def, i) => {
    const evs = byStation.get(def.key) ?? [];
    const enters = changes.filter((c) => def.statuses.includes(c.to));
    const lastEnter = enters[enters.length - 1];
    const leftAt = lastEnter ? changes.find((c) => c.tsUs > lastEnter.tsUs && !def.statuses.includes(c.to))?.tsUs : undefined;

    let state: Tdm112State = 'idle';
    if (status === 'shipped') state = 'done';
    else if (ended) state = i < stoppedAt ? 'done' : i === stoppedAt ? (status === 'failed' ? 'failed' : 'idle') : 'idle';
    else if (cur >= 0) state = i < cur ? 'done' : i === cur ? 'working' : 'idle';

    const facts: string[] = [];
    let chips: Tdm112Chip[] | undefined;
    let chipsTotal: number | undefined;
    let computeTag: Tdm112StationView['computeTag'];
    let waitingText: string | undefined;
    switch (def.key) {
      case 'baseline':
      case 'eval': {
        const r = def.key === 'baseline' ? baselineRun : candRun;
        facts.push(...evalFacts(r));
        chips = chipsOf(r);
        chipsTotal = r?.trials;
        // RDF_SEED_EVALS demo run-through: outcomes drawn at the sim policy's rate, no robot trials.
        if (r && trials.some((t) => t.evalRunId === r.id && (t.failureNote ?? '').startsWith('[SEEDED]'))) computeTag = 'SEEDED';
        if (state === 'working' && openResets > 0) waitingText = `Waiting for a contributor to reset the scene (${plural(openResets, 'open reset')})`;
        break;
      }
      case 'diagnose':
        if (shownClusters.length) facts.push(plural(shownClusters.length, 'cluster'), tdm112Clip(`${shownClusters[0].label} ×${shownClusters[0].count}`, 40));
        break;
      case 'plan_data':
        if (plan) {
          facts.push(`${plan.amount} ${unitOf(plan.dataType, plan.amount)}`, `est. ${usd(plan.estCostCents)}`);
          if (state === 'working' && plan.status === 'proposed') waitingText = `Waiting for operator approval of ${usd(plan.estCostCents)}`;
        }
        break;
      case 'collect':
        if (dataTasks.length) facts.push(`${openTasks} open`, `${claimedTasks} claimed`, `${doneTasks} done`);
        if (drops) facts.push(plural(drops, 'drop'));
        else if (drafted) facts.push(`${drafted} drafted`);
        if (state === 'working' && openTasks > 0 && claimedTasks === 0) waitingText = `Waiting for contributors: ${plural(openTasks, 'open task')}`;
        break;
      case 'review':
        if (dataReviews.length || pendingReview) facts.push(`${accepted} accepted`, `${rejected} rejected`, ...(pendingReview ? [`${pendingReview} pending`] : []));
        break;
      case 'train':
        if (run) {
          const hp = parseObj(run.hyperparamsJson);
          const m = parseObj(run.metricsJson);
          const total = n(hp.num_train_steps) ?? n(hp.steps) ?? n(hp.train_steps);
          const step = n(m.step) ?? n(m.steps_done) ?? n(m.global_step);
          if (total) facts.push(step !== undefined ? `step ${step.toLocaleString('en-US')}/${total.toLocaleString('en-US')}` : `${total.toLocaleString('en-US')} steps`);
          facts.push(run.status);
          const ck = run.checkpointUri ?? '';
          const stub = (run.modalCallId ?? '').startsWith('stub-') || m.fake === true || ck.startsWith('fake://') || ck.includes('stub-no-gpu');
          computeTag = stub ? 'STUB' : run.modalCallId ? 'REAL' : undefined;
        }
        break;
      case 'ship':
        facts.push(`target ${Math.round(ticket.targetSuccess * 100)}%`);
        if (ticket.deployedCheckpointUri) facts.push('deployed');
        break;
    }
    if (waitingText) state = 'waiting';

    const last = evs[evs.length - 1];
    let activity: string;
    let activityIsError = false;
    if (waitingText) activity = waitingText;
    else if (last) {
      activity = tdm112Clip(tdm112Words(last));
      activityIsError = isErrorType(last.type);
    } else if (state === 'working' && lastEnter) {
      const tr = [...events].reverse().find((e) => e.type === 'flywheel.transition' && def.statuses.includes(s(e.payload.to)));
      activity = tdm112Clip(tr && s(tr.payload.reason) ? `Started: ${s(tr.payload.reason)}` : `${STATUS_WORD[lastEnter.to] ?? def.label} started`);
    } else activity = state === 'done' ? 'Done' : def.waitsFor;

    const startedUs = lastEnter?.tsUs;
    const endedUs = state === 'working' || state === 'waiting' ? undefined : leftAt;
    return {
      key: def.key,
      index: i,
      label: def.label,
      sub: def.sub,
      agent: def.agent,
      state,
      activity,
      activityTsUs: waitingText ? undefined : last?.tsUs,
      activityIsError,
      facts,
      chips,
      chipsTotal,
      llm: def.llmStep ? llmFor(def.llmStep, startedUs ?? 0) : undefined,
      computeTag,
      startedUs,
      endedUs,
      runs: enters.length,
      waitsFor: def.waitsFor,
      eventIds: evs.map((e) => e.id),
    };
  });

  const activeIndex = ended ? -1 : cur;

  const droids = tdm112Droids(input.sessions ?? [], input.resumes ?? [], nowUs, input.describePredicate);

  return {
    ticketId: Number(ticket.id),
    stations,
    activeIndex,
    iteration,
    maxIterations,
    iterationLabel: iteration > 0 ? `Iteration ${Math.min(iteration + 1, Math.max(maxIterations, iteration + 1))} of ${Math.max(maxIterations, iteration + 1)}` : undefined,
    ended,
    endedNote,
    queued: !ended && cur < 0,
    droids,
  };
}

/**
 * Droids: each stands at its station while it acts, sleeps there while parked on a predicate, and waits
 * in the bay at the head of the line otherwise. Exported for /factory's shared overhead rail.
 */
export function tdm112Droids(
  sessions: readonly Tdm112Session[],
  resumes: readonly Tdm112Event[],
  nowUs: number,
  describePredicate?: (json: string) => string,
): Tdm112DroidView[] {
  return sessions
    .filter((x) => tdm112IsDroidRole(x.role))
    .map((x) => {
      const home = DROID_HOME[x.role] ?? 3;
      const st = x.status;
      const myResumes = resumes.filter((r) => n(r.payload.session_id) === Number(x.id));
      const wake = [...myResumes].reverse().map((r) => ({ r, on: tdm112WokeOn(r) })).find((w) => w.on);
      const woke = wake ? { tsUs: wake.r.tsUs, on: wake.on as string, recent: nowUs - wake.r.tsUs < 10_000_000 } : undefined;
      let state: Tdm112DroidState;
      if (st === 'waiting_for') state = 'asleep';
      else if (st === 'resuming' || (woke?.recent && st !== 'done' && st !== 'failed')) state = 'waking';
      else if (st === 'running' || st === 'starting') state = 'working';
      else if (st === 'failed') state = 'failed';
      else if (st === 'done') state = 'done';
      else state = 'docked';
      let raw: string | undefined;
      try {
        const o = x.waitingForJson ? JSON.parse(x.waitingForJson) : null;
        raw = o && typeof o === 'object' ? (typeof o.raw === 'string' ? o.raw : typeof o.predicate === 'string' ? o.predicate : undefined) : undefined;
      } catch {
        raw = undefined;
      }
      const atStation = state === 'working' || state === 'asleep' || state === 'waking' || state === 'failed';
      return {
        id: Number(x.id),
        ticketId: Number(x.ticketId),
        role: x.role,
        label: DROID_LABEL[x.role] ?? x.role.replace(/_/g, ' '),
        name: x.name,
        stationIndex: atStation ? home : -1,
        homeIndex: home,
        state,
        step: tdm112Clip(tidy(x.currentStep || ''), 90),
        predicate:
          st === 'waiting_for' && x.waitingForJson
            ? { raw, words: describePredicate ? describePredicate(x.waitingForJson) : raw ?? 'waiting' }
            : undefined,
        woke,
      };
    })
    .sort((a, b) => a.homeIndex - b.homeIndex || a.id - b.id);
}

// ------------------------------------------------------------------------------ handoffs

/**
 * The part on the conveyor: when the active station moves, a part travels from the old station to the
 * new one (backwards on an eval loop-back). Null when nothing moved or nothing ran before.
 */
export function tdm112Handoff(prev: number, next: number): { from: number; to: number; back: boolean } | null {
  if (prev < 0 || next < 0 || prev === next) return null;
  return { from: prev, to: next, back: next < prev };
}

/** "4m 12s", "1h 03m", "38s". */
export function tdm112Elapsed(fromUs: number | undefined, toUs: number): string {
  if (!fromUs || toUs < fromUs) return '';
  const sec = Math.floor((toUs - fromUs) / 1e6);
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  if (m < 60) return `${m}m ${String(sec % 60).padStart(2, '0')}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`;
}
