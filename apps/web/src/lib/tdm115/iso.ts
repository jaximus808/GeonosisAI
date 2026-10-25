// TDM-115: the isometric factory's view model. Pure (no React, no side effects), so
// `pnpm --filter @rdf/web test` (test/tdm115-iso.test.ts) runs it under plain Node.
//
// In: the TDM-112 floor model of one ticket (lib/tdm112/floor.ts) plus the few raw rows the machines
// draw (eval trials, failure clusters, the data plan, tasks, reviews, training runs, robot commands).
// Out: one robot cell. Seven machines around the robot, nine agents standing at them, each with a state
// colour and one short first-person line, and the parts on the conveyor with their counts.
// Nothing is invented: an agent with nothing to do says what it waits for.
//
// TDM-114 (robot arbitration) adds a waiting-for-robot state, the per-robot queue and the live eval phase
// to the floor model. This module reads those optionally (see `loose*` below) and derives the robot holder
// from robot_command rows itself until they land, so it works before and after.

import type { Tdm112DroidView, Tdm112Event, Tdm112Floor, Tdm112StationView, TsLike } from '../tdm112/floor';

// ------------------------------------------------------------------------------ inputs

export interface Tdm115Robot { id: bigint; name: string; status: string; cameraConfigJson: string }
export interface Tdm115Command { id: bigint; robotId: bigint; ticketId: bigint; type: string; payloadJson: string; status: string; issuedAt: TsLike }
export interface Tdm115TicketLite { id: bigint; robotId: bigint; status: string; taskDescription: string; createdAt: TsLike; createdBy?: string; iteration?: number; targetSuccess?: number }
export interface Tdm115Trial { evalRunId: bigint; trialIndex: number; success: boolean; failureNote?: string | null }
export interface Tdm115EvalRun { id: bigint; kind: string; status: string; trials: number; completed: number; successes: number; createdAt: TsLike }
export interface Tdm115Cluster { iteration: number; label: string; count: number }
export interface Tdm115Plan { id: bigint; iteration: number; dataType: string; amount: number; estCostCents: bigint; approvedCents: bigint; status: string; createdAt: TsLike; reasoning?: string }
export interface Tdm115Task { id: bigint; type: string; status: string; dataPlanId: bigint; priceCents: bigint }
export interface Tdm115Episode { id: bigint; kind: string; reviewStatus: string }
export interface Tdm115Review { episodeId: bigint; accepted: boolean }
export interface Tdm115TrainingRun { id: bigint; status: string; checkpointUri?: string | null; createdAt: TsLike }

export interface Tdm115Input {
  floor: Tdm112Floor;
  ticket: Tdm115TicketLite;
  robot?: Tdm115Robot | null;
  /** Every robot_command row of this robot (any ticket). */
  commands?: readonly Tdm115Command[];
  /** Every ticket on this robot, for the queue and to name the holder. */
  robotTickets?: readonly Tdm115TicketLite[];
  evalRuns?: readonly Tdm115EvalRun[];
  evalTrials?: readonly Tdm115Trial[];
  clusters?: readonly Tdm115Cluster[];
  plans?: readonly Tdm115Plan[];
  tasks?: readonly Tdm115Task[];
  episodes?: readonly Tdm115Episode[];
  reviews?: readonly Tdm115Review[];
  trainingRuns?: readonly Tdm115TrainingRun[];
  /** This ticket's event_log rows, oldest first (useTdm112Floor's `events`). */
  events?: readonly Tdm112Event[];
  /** llm.call rows, oldest first (they carry no ticket id). */
  llmCalls?: readonly Tdm112Event[];
  nowUs: number;
  /** AgentWorkbench liveness per TDM-112 station index (lib/agentwork/workbench.ts awLiveness + headline), so a
   * figure only animates while its station really shows activity. Without it, the station state decides. */
  liveness?: readonly (Tdm115Liveness | undefined)[];
}

/** The slice of the AgentWorkbench model this module reads (structural; awWorkbench satisfies it). */
export interface Tdm115Liveness {
  mode: 'working' | 'thinking' | 'waiting' | 'stalled' | 'done' | 'failed' | 'idle';
  /** The newest narrated line (agent.step / agent.thought) of the station, if any. */
  narration?: string;
  ageS?: number;
}

// ------------------------------------------------------------------------------ outputs

export type Tdm115MachineKey = 'eval' | 'diagnose' | 'plan' | 'collect' | 'review' | 'train' | 'ship';
/** working melt, waiting for the robot ochre (dashed), waiting for a person ochre, idle steel, done ink, failed slag, asleep steel. */
export type Tdm115State = 'working' | 'waiting_robot' | 'waiting_person' | 'idle' | 'done' | 'failed' | 'asleep' | 'stalled';

export const TDM115_STATE_WORDS: Record<Tdm115State, string> = {
  working: 'Working',
  waiting_robot: 'Waiting for robot',
  waiting_person: 'Waiting for a person',
  idle: 'Idle',
  done: 'Done',
  failed: 'Failed',
  asleep: 'Asleep',
  stalled: 'No recent activity',
};

export interface Tdm115Agent {
  key: string;
  name: string;
  /** service = plain code; claude = reasons with Claude; droid = a long-running Factory Droid. */
  kind: 'service' | 'claude' | 'droid';
  machine: Tdm115MachineKey;
  state: Tdm115State;
  /** One short first-person line from live rows. */
  say: string;
  /** For Claude agents: the model of the newest llm.call of its step (or 'Claude' before any call). */
  model?: string;
  /** For a sleeping droid: the raw WAITING_FOR predicate. */
  predicate?: string;
  /** A droid woke in the last 10 s (SpacetimeDB row change): pulse. */
  woke?: string;
  /** Where the figure stands: its machine, or the droid bay when a droid is not acting. */
  at: Tdm115MachineKey | 'bay';
  does: string;
  wakes: string;
  /** TDM-112 station indexes this agent serves (for onStationClick). */
  stationIndex: number;
  /** A Claude call is in flight right now (the figure shows thinking dots). */
  thinking?: boolean;
}

export interface Tdm115Lights { trials: number; marks: ('pass' | 'fail' | 'todo' | 'now')[]; passed: number; done: boolean; seeded: boolean }

export interface Tdm115Machine {
  key: Tdm115MachineKey;
  label: string;
  state: Tdm115State;
  /** Two or three short numbers for the plate ("3/10", "$17.50"). */
  readout: string[];
  /** TDM-112 station index to open on click. */
  stationIndex: number;
}

export interface Tdm115Part { key: 'eval_runs' | 'episodes' | 'accepted' | 'checkpoints'; label: string; count: number; between: [Tdm115MachineKey, Tdm115MachineKey] }

export interface Tdm115Holder { ticketId: number; commandId: number; type: string; purpose: string; sinceUs: number; paused: boolean; words: string }

export interface Tdm115Cell {
  ticketId: number;
  robotId: number;
  robotName: string;
  sim: boolean;
  machines: Record<Tdm115MachineKey, Tdm115Machine>;
  agents: Tdm115Agent[];
  parts: Tdm115Part[];
  lights: { baseline: Tdm115Lights | null; finetuned: Tdm115Lights | null };
  bins: { label: string; count: number }[];
  plan: { id: number; words: string; priceCents: number; status: string; waiting: boolean; rationale?: string } | null;
  collect: { open: number; claimed: number; done: number; discord: number; text: number; drafted: number };
  review: { accepted: number; rejected: number; pending: number };
  train: { tag?: 'STUB' | 'REAL'; status?: string; step?: string };
  ship: { target: number; shipped: boolean; deployed: boolean };
  holder: Tdm115Holder | null;
  /** The robot is held by another ticket while this ticket needs it. */
  waitingForRobot: boolean;
  /** The station running now, -1 none. */
  activeIndex: number;
  /** The machine running now. */
  active: Tdm115MachineKey | null;
  /** Bottom-right line of the plate. */
  readout: string;
}

// ------------------------------------------------------------------------------ helpers

const s = (v: unknown): string => (v === undefined || v === null ? '' : String(v));
const n = (v: unknown): number | undefined => {
  const x = typeof v === 'number' ? v : typeof v === 'string' && v.trim() !== '' ? Number(v) : typeof v === 'bigint' ? Number(v) : NaN;
  return Number.isFinite(x) ? x : undefined;
};
const us = (t: TsLike | null | undefined) => (t ? Number(t.microsSinceUnixEpoch) : 0);
export const tdm115Usd = (cents: number | bigint) => `$${(Number(cents) / 100).toFixed(2)}`;
const plural = (k: number, one: string, many = `${one}s`) => `${k} ${k === 1 ? one : many}`;
export const tdm115Tkt = (id: number | bigint) => `TKT-${String(id).padStart(4, '0')}`;
function parseObj(text: string | null | undefined): Record<string, unknown> {
  if (!text) return {};
  try {
    const v = JSON.parse(text);
    return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}
const clip = (t: string, max = 80) => (Array.from(t).length <= max ? t : `${Array.from(t).slice(0, max - 1).join('').replace(/\s+\S*$/, '')}…`);
const UNIT: Record<string, [string, string]> = {
  recovery_takeover: ['recovery takeover', 'recovery takeovers'],
  clean_teleop: ['clean demo', 'clean demos'],
  purchased: ['bought episode', 'bought episodes'],
  reset: ['reset', 'resets'],
};
const unit = (type: string, k: number) => (UNIT[type] ? UNIT[type][k === 1 ? 0 : 1] : type.replace(/_/g, ' '));
const newest = <T extends { createdAt: TsLike }>(rows: readonly T[]) => [...rows].sort((a, b) => us(b.createdAt) - us(a.createdAt))[0];

export const tdm115IsSim = (robot: Pick<Tdm115Robot, 'cameraConfigJson'> | null | undefined) => parseObj(robot?.cameraConfigJson).sim === true;

/** Machine of each TDM-112 station index (both Eval stations are the one bench by the robot). */
export const TDM115_MACHINE_OF_STATION: readonly Tdm115MachineKey[] = ['eval', 'diagnose', 'plan', 'collect', 'review', 'train', 'eval', 'ship'];

// TDM-114 fields, read loosely: the floor model may grow a `waiting_robot`-like station state, an
// `evalPhase`/`phase` on the eval station and a `robotQueue`. Unknown shapes are ignored.
type Loose = Record<string, unknown>;
const looseState = (st: Tdm112StationView): Tdm115State | null => {
  const raw = String(st.state);
  if (raw === 'working' || raw === 'idle' || raw === 'done' || raw === 'failed') return raw;
  if (raw === 'waiting') return 'waiting_person';
  if (/robot/.test(raw)) return 'waiting_robot';
  return null;
};
const loosePhase = (st: Tdm112StationView | undefined): string | undefined => {
  if (!st) return undefined;
  const o = st as unknown as Loose;
  const p = o.phase ?? o.evalPhase ?? (o.live as Loose | undefined)?.phase;
  return typeof p === 'string' && p ? p : undefined;
};

// ------------------------------------------------------------------------------ robot holder

const JOB_TYPES = new Set(['run_baseline', 'run_eval', 'start_teleop', 'reset_request', 'home', 'load_checkpoint']);
const OPEN = new Set(['issued', 'acked']);
const JOB_WORDS: Record<string, string> = {
  run_eval: 'its eval',
  run_baseline: 'its baseline',
  start_teleop: 'a teleop session',
  reset_request: 'a scene reset',
  home: 'homing the arm',
  load_checkpoint: 'loading a checkpoint',
};

/** The open job on a robot (the TDM-13 job lock), or null. */
export function tdm115Holder(robot: Tdm115Robot | null | undefined, commands: readonly Tdm115Command[]): Tdm115Holder | null {
  if (!robot) return null;
  const open = commands
    .filter((c) => c.robotId === robot.id && OPEN.has(c.status) && JOB_TYPES.has(c.type))
    .sort((a, b) => us(a.issuedAt) - us(b.issuedAt));
  const c = open[0];
  if (!c) return null;
  const p = parseObj(c.payloadJson);
  const purpose = s(p.purpose) || (c.type === 'run_baseline' ? 'baseline' : c.type === 'run_eval' ? 'eval' : c.type);
  const paused = robot.status === 'paused';
  const job = purpose === 'collect_recovery' ? 'a collection run' : JOB_WORDS[c.type] ?? c.type.replace(/_/g, ' ');
  const who = Number(c.ticketId) > 0 ? tdm115Tkt(c.ticketId) : 'Another job';
  const words = paused ? `${who} has it, paused for a person to take over` : `${who} is using it for ${job}`;
  return { ticketId: Number(c.ticketId), commandId: Number(c.id), type: c.type, purpose, sinceUs: us(c.issuedAt), paused, words };
}

export interface Tdm115QueueItem { ticketId: number; status: string; label: string; now: boolean; task: string }

const ENDED = new Set(['shipped', 'failed', 'cancelled']);
const STAGE: Record<string, string> = {
  created: 'Queued', onboarding: 'Queued', baseline: 'Baseline', diagnose: 'Diagnose', plan_data: 'Plan data', dispatch: 'Collect',
  review: 'Review', train: 'Train', eval: 'Eval', shipped: 'Shipped', failed: 'Failed', cancelled: 'Cancelled',
};
export const tdm115Stage = (status: string) => STAGE[status] ?? status.replace(/_/g, ' ');

/** A robot's tickets: the one on the arm now first, then the rest oldest first. Ended tickets are left out. */
export function tdm115RobotQueue(tickets: readonly Tdm115TicketLite[], holder: Tdm115Holder | null): Tdm115QueueItem[] {
  const live = tickets.filter((t) => !ENDED.has(t.status)).sort((a, b) => us(a.createdAt) - us(b.createdAt));
  const nowId = holder?.ticketId ?? 0;
  const items = live.map((t) => ({ ticketId: Number(t.id), status: t.status, label: tdm115Stage(t.status), now: Number(t.id) === nowId, task: t.taskDescription }));
  return [...items.filter((i) => i.now), ...items.filter((i) => !i.now)];
}

/** Which ticket a robot cell focuses on: the holder's, else the newest live ticket, else the newest ended one. */
export function tdm115FocusTicket(tickets: readonly Tdm115TicketLite[], holder: Tdm115Holder | null): Tdm115TicketLite | undefined {
  const real = tickets.filter((t) => !(t.createdBy ?? '').startsWith('test.'));
  const pool = real.length ? real : tickets;
  const byNew = [...pool].sort((a, b) => us(b.createdAt) - us(a.createdAt));
  return (holder ? byNew.find((t) => Number(t.id) === holder.ticketId) : undefined) ?? byNew.find((t) => !ENDED.has(t.status)) ?? byNew[0];
}

// ------------------------------------------------------------------------------ the cell

const DROID_INFO: Record<string, { does: string; wakes: string; machine: Tdm115MachineKey }> = {
  onboarding_droid: { does: 'Connects a new robot and checks its cameras and joints.', wakes: 'Wakes when a robot row comes online.', machine: 'eval' },
  data_droid: { does: 'Watches collection and asks for more data when episodes fall short.', wakes: 'Wakes when the accepted episode count changes.', machine: 'collect' },
  training_droid: { does: 'Starts the fine-tune and watches it on Modal.', wakes: 'Wakes when enough episodes are accepted.', machine: 'train' },
};
const DROID_ORDER = ['onboarding_droid', 'data_droid', 'training_droid'] as const;
const DROID_NAME: Record<string, string> = { onboarding_droid: 'Onboarding Droid', data_droid: 'Data Droid', training_droid: 'Training Droid' };

export function tdm115Cell(input: Tdm115Input): Tdm115Cell {
  const { floor, ticket, robot, nowUs } = input;
  const st = floor.stations;
  const tid = Number(ticket.id);
  const evalRuns = input.evalRuns ?? [];
  const trials = input.evalTrials ?? [];
  const events = input.events ?? [];
  const holderRaw = tdm115Holder(robot, input.commands ?? []);
  const holder = holderRaw;
  const sim = tdm115IsSim(robot);
  const arm = sim ? 'the sim arm' : robot ? robot.name : 'the robot';

  const stateOf = (i: number): Tdm115State => looseState(st[i]) ?? 'idle';

  // ---- eval bench (baseline index 0, fine-tuned index 6)
  const lightsOf = (kind: 'baseline' | 'finetuned'): Tdm115Lights | null => {
    const run = newest(evalRuns.filter((r) => (kind === 'baseline' ? r.kind === 'baseline' : r.kind !== 'baseline')));
    if (!run) return null;
    const mine = trials.filter((t) => t.evalRunId === run.id);
    const by = new Map(mine.map((t) => [t.trialIndex, t]));
    const running = run.status === 'running' || run.status === 'queued';
    const marks = Array.from({ length: Math.max(run.trials, 0) }, (_, i) => {
      const t = by.get(i);
      if (t) return t.success ? 'pass' : 'fail';
      return running && i === mine.length ? 'now' : 'todo';
    }) as Tdm115Lights['marks'];
    return {
      trials: run.trials,
      marks,
      passed: mine.filter((t) => t.success).length,
      done: run.status === 'done',
      seeded: mine.some((t) => (t.failureNote ?? '').startsWith('[SEEDED]')),
    };
  };
  const baseline = lightsOf('baseline');
  const finetuned = lightsOf('finetuned');

  const evalIdx = floor.activeIndex === 6 ? 6 : floor.activeIndex === 0 ? 0 : st[6] && st[6].state !== 'idle' ? 6 : 0;
  const evalStation = st[evalIdx];
  const evalLights = evalIdx === 6 ? finetuned : baseline;
  const evalWorking = stateOf(evalIdx) === 'working' || stateOf(evalIdx) === 'waiting_robot';
  // Waiting for the robot: this ticket's eval stage is on, another job holds the arm and our run is not running.
  const runningRun = evalRuns.find((r) => r.status === 'running');
  const heldByOther = !!holder && holder.ticketId !== tid;
  const waitingForRobot = stateOf(evalIdx) === 'waiting_robot' || (evalWorking && heldByOther && !runningRun);

  const phase = loosePhase(evalStation) ?? (() => {
    const e = [...events].reverse().find((x) => /phase/.test(x.type) && typeof x.payload.phase === 'string');
    return e && nowUs - e.tsUs < 30_000_000 ? s(e.payload.phase) : undefined;
  })();

  let evalState: Tdm115State = stateOf(evalIdx);
  if (waitingForRobot) evalState = 'waiting_robot';
  let evalSay: string;
  const kindWord = evalIdx === 6 ? 'eval' : 'baseline';
  if (waitingForRobot && holder) evalSay = `Waiting for the robot. ${holder.words}.`;
  else if (waitingForRobot) evalSay = 'Waiting for the robot.';
  else if (evalState === 'waiting_person') evalSay = 'I need a person to reset the scene.';
  else if (evalState === 'working' && evalLights) {
    const doneN = evalLights.marks.filter((m) => m === 'pass' || m === 'fail').length;
    if (evalLights.seeded && evalLights.done) evalSay = `Seeded ${evalLights.trials} trials: ${evalLights.passed} passed.`;
    else if (doneN >= evalLights.trials) evalSay = `All ${evalLights.trials} trials in: ${evalLights.passed} passed.`;
    else evalSay = `Running trial ${doneN + 1} of ${evalLights.trials} on ${arm}${phase ? `: ${phase}` : ''}.`;
  } else if (evalState === 'working') evalSay = `Starting the ${kindWord} on ${arm}.`;
  else if (evalState === 'failed') evalSay = clip(st[evalIdx]?.activity ?? 'The eval failed.');
  else if (finetuned?.done) evalSay = `Fine-tuned: ${finetuned.passed} of ${finetuned.trials} passed${baseline?.done ? `, up from ${baseline.passed}` : ''}.`;
  else if (baseline?.done) evalSay = `Baseline: ${baseline.passed} of ${baseline.trials} passed.`;
  else evalSay = 'I run the trials once the robot is onboarded.';

  // ---- diagnose
  const iteration = ticket.iteration ?? floor.iteration;
  const clustersAll = input.clusters ?? [];
  const itC = clustersAll.filter((c) => c.iteration === iteration);
  const lastIt = Math.max(-1, ...clustersAll.map((c) => c.iteration));
  const clusters = (itC.length ? itC : clustersAll.filter((c) => c.iteration === lastIt)).slice().sort((a, b) => b.count - a.count);
  const bins = clusters.slice(0, 4).map((c) => ({ label: c.label, count: c.count }));
  const failures = clusters.reduce((a, c) => a + c.count, 0);
  const diagState = stateOf(1);
  const diagSay =
    diagState === 'working'
      ? clusters.length
        ? `I grouped ${plural(failures, 'failure')} into ${plural(clusters.length, 'cause')}.`
        : 'Reading the failed trials to find the causes.'
      : diagState === 'failed'
        ? clip(st[1]?.activity ?? 'Diagnosis failed.')
        : clusters.length
          ? `I grouped ${plural(failures, 'failure')} into ${plural(clusters.length, 'cause')}.`
          : 'I wait for an eval with failed trials.';

  // ---- plan
  const plans = input.plans ?? [];
  const p = newest(plans.filter((x) => x.iteration === iteration)) ?? newest(plans);
  const planState0 = stateOf(2);
  const planWaiting = !!p && p.status === 'proposed' && (planState0 === 'working' || planState0 === 'waiting_person');
  const planState: Tdm115State = planWaiting ? 'waiting_person' : planState0;
  const planWords = p ? `${p.amount} ${unit(p.dataType, p.amount)}` : '';
  const plan = p
    ? { id: Number(p.id), words: planWords, priceCents: Number(p.estCostCents), status: p.status, waiting: planWaiting, rationale: p.reasoning }
    : null;
  const planSay = planWaiting
    ? `Waiting for you to approve ${tdm115Usd(p!.estCostCents)}.`
    : planState === 'working'
      ? 'Working out which data fixes these failures.'
      : planState === 'failed'
        ? clip(st[2]?.activity ?? 'Planning failed.')
        : p
          ? `Buy ${planWords} for ${tdm115Usd(p.approvedCents > 0n ? p.approvedCents : p.estCostCents)}${p.approvedCents > 0n ? ', approved' : ''}.`
          : 'I plan the data once the causes are known.';

  // ---- collect
  const tasks = input.tasks ?? [];
  const dataTasks = tasks.filter((t) => t.type !== 'reset' && (!p || t.dataPlanId === p.id || t.dataPlanId === 0n));
  const open = dataTasks.filter((t) => t.status === 'open').length;
  const claimed = dataTasks.filter((t) => t.status === 'claimed' || t.status === 'submitted').length;
  const done = dataTasks.filter((t) => t.status === 'completed').length;
  const dataTaskIds = new Set(tasks.filter((t) => t.type !== 'reset').map((t) => Number(t.id)));
  const forData = (e: Tdm112Event) => {
    const ids = Array.isArray(e.payload.task_ids) ? (e.payload.task_ids as unknown[]) : [e.payload.task_id];
    return ids.some((x) => dataTaskIds.has(n(x) ?? -1));
  };
  const drops = events.filter((e) => e.type === 'job_drop.posted' && forData(e));
  const channel = (e: Tdm112Event) => s(e.payload.channel).toLowerCase();
  const discord = drops.filter((e) => channel(e).includes('discord')).length;
  const text = drops.filter((e) => /text|sms|imessage|inkbox/.test(channel(e))).length;
  const drafted = events.filter((e) => e.type === 'job_drop.preview' && forData(e)).length;
  const colState0 = stateOf(3);
  const colState: Tdm115State = colState0 === 'working' && open > 0 && claimed === 0 ? 'waiting_person' : colState0;
  const colSay =
    colState === 'waiting_person'
      ? `Waiting for contributors: ${plural(open, 'open task')}.`
      : colState === 'working'
        ? dataTasks.length
          ? `${claimed} claimed, ${done} of ${dataTasks.length} done.`
          : 'Posting the paid tasks.'
        : dataTasks.length
          ? `${done} of ${dataTasks.length} tasks done.`
          : 'I post paid tasks once you approve a plan.';

  // ---- review (reviewer = per-episode, dataset QA = whole set)
  const episodes = (input.episodes ?? []).filter((e) => e.kind !== 'baseline' && e.kind !== 'eval');
  const epIds = new Set(episodes.map((e) => e.id));
  const reviews = (input.reviews ?? []).filter((r) => epIds.has(r.episodeId));
  const accepted = reviews.filter((r) => r.accepted).length;
  const rejected = reviews.length - accepted;
  const pending = episodes.filter((e) => e.reviewStatus === 'pending' || e.reviewStatus === 'not_reviewed').length;
  const revState = stateOf(4);
  const revSay =
    revState === 'working' && pending > 0
      ? `Reviewing ${plural(pending, 'episode')}. ${accepted} passed, ${rejected} rejected.`
      : reviews.length
        ? `${accepted} passed, ${rejected} rejected.`
        : revState === 'working'
          ? 'Waiting for recorded episodes.'
          : 'I watch every recorded episode.';
  const qaEv = [...events].reverse().find((e) => e.type.startsWith('qa.'));
  const qaRate = qaEv ? n(qaEv.payload.acceptance_rate) ?? n(qaEv.payload.rate) : undefined;
  const qaSay = qaEv
    ? qaEv.type === 'qa.failed' || qaEv.type === 'qa.error'
      ? clip(`Dataset check failed${s(qaEv.payload.reason) ? `: ${s(qaEv.payload.reason)}` : ''}.`)
      : qaRate !== undefined
        ? `Dataset checked: ${Math.round(qaRate <= 1 ? qaRate * 100 : qaRate)}% accepted.`
        : 'Dataset checked.'
    : 'I check the whole dataset before training.';
  const qaState: Tdm115State = qaEv ? (qaEv.type === 'qa.failed' || qaEv.type === 'qa.error' ? 'failed' : revState === 'working' ? 'working' : 'done') : revState === 'done' ? 'done' : 'idle';

  // ---- train
  const trainSt = st[5];
  const tr = newest(input.trainingRuns ?? []);
  const step = trainSt?.facts.find((f) => f.startsWith('step ') || f.endsWith(' steps'));
  const train = { tag: trainSt?.computeTag === 'STUB' || trainSt?.computeTag === 'REAL' ? trainSt.computeTag : undefined, status: tr?.status, step };
  const checkpoints = (input.trainingRuns ?? []).filter((r) => !!r.checkpointUri).length;

  // ---- ship
  const shipState = stateOf(7);
  const ship = { target: Math.round((ticket.targetSuccess ?? 0) * 100), shipped: floor.ended === 'shipped', deployed: (st[7]?.facts ?? []).includes('deployed') };

  // ---- llm badges
  const modelOf = (step: string): string => {
    const calls = (input.llmCalls ?? []).filter((c) => s(c.payload.step) === step);
    const c = calls[calls.length - 1];
    const m = c ? s(c.payload.served_model) || s(c.payload.model) : '';
    return m ? m.replace(/^claude-/, '').replace(/-\d{8}$/, '') : 'Claude';
  };

  const agents: Tdm115Agent[] = [
    { key: 'eval_runner', name: 'Eval runner', kind: 'service', machine: 'eval', at: 'eval', state: evalState, say: evalSay, stationIndex: evalIdx,
      does: 'Runs the policy on the robot for each trial and scores it.', wakes: 'Starts when a ticket reaches Baseline or Eval.' },
    { key: 'clusterer', name: 'Failure clusterer', kind: 'claude', model: modelOf('diagnose'), machine: 'diagnose', at: 'diagnose', state: diagState, say: diagSay, stationIndex: 1,
      does: 'Watches the failed trials and groups them by cause.', wakes: 'Starts when an eval finishes with failures.' },
    { key: 'planner', name: 'Planner', kind: 'claude', model: modelOf('planner'), machine: 'plan', at: 'plan', state: planState, say: planSay, stationIndex: 2,
      does: 'Decides what data to buy, how much, and at what price.', wakes: 'Starts when the causes are written.' },
    { key: 'dispatcher', name: 'Dispatcher', kind: 'service', machine: 'collect', at: 'collect', state: colState, say: colSay, stationIndex: 3,
      does: 'Posts paid tasks to contributors on Discord and by text.', wakes: 'Starts when you approve the spend.' },
    { key: 'reviewer', name: 'Reviewer', kind: 'claude', model: modelOf('review'), machine: 'review', at: 'review', state: revState, say: revSay, stationIndex: 4,
      does: 'Checks each recorded episode and accepts or rejects it.', wakes: 'Starts when an episode is uploaded.' },
    { key: 'dataset_qa', name: 'Dataset QA', kind: 'service', machine: 'review', at: 'review', state: qaState, say: qaSay, stationIndex: 4,
      does: 'Checks the whole dataset for gaps before training.', wakes: 'Starts when collection reaches its target.' },
  ];

  // AgentWorkbench liveness: a working figure whose station has gone quiet is stalled, not busy; one whose
  // Claude call is in flight is thinking; a narrated line beats the generic sentence.
  const lv = input.liveness;
  if (lv) {
    for (const a of agents) {
      const l = lv[a.stationIndex];
      if (!l || a.state !== 'working') continue;
      if (l.mode === 'stalled') {
        a.state = 'stalled';
        a.say = clip(`No activity for ${Math.round((l.ageS ?? 0) / 60)} min. It may be stuck.`);
      } else if (l.mode === 'thinking') {
        a.thinking = true;
        a.say = clip(l.narration && !/^Asking Claude/.test(l.narration) ? l.narration : 'Thinking it through with Claude…', 90);
      } else if (l.narration) a.say = clip(l.narration, 90);
    }
  }

  // Factory Droids: always three figures; a droid with no session for this ticket waits in the bay.
  const byRole = new Map<string, Tdm112DroidView>();
  for (const d of floor.droids) if (!byRole.has(d.role) || d.state !== 'docked') byRole.set(d.role, d);
  for (const role of DROID_ORDER) {
    const d = byRole.get(role);
    const info = DROID_INFO[role];
    let state: Tdm115State = 'idle';
    let say = 'Not started for this ticket.';
    let predicate: string | undefined;
    let woke: string | undefined;
    if (d) {
      if (d.state === 'asleep') {
        state = 'asleep';
        predicate = d.predicate?.raw ?? d.predicate?.words;
        say = predicate ? `zz WAITING_FOR ${predicate}` : 'zz asleep';
      } else if (d.state === 'working' || d.state === 'waking') {
        state = 'working';
        say = d.step || 'Working.';
      } else if (d.state === 'failed') {
        state = 'failed';
        say = d.step || 'Failed.';
      } else if (d.state === 'done') {
        state = 'done';
        say = 'Done. Back in the bay.';
      } else say = 'In the bay.';
      if (d.woke?.recent) woke = d.woke.on;
    }
    const acting = state === 'working' || state === 'asleep' || state === 'failed';
    agents.push({
      key: role, name: DROID_NAME[role], kind: 'droid', machine: info.machine, at: acting ? info.machine : 'bay', state, say: clip(say, 90),
      predicate, woke, stationIndex: d?.homeIndex ?? TDM115_MACHINE_OF_STATION.indexOf(info.machine), does: info.does, wakes: info.wakes,
    });
  }

  const label: Record<Tdm115MachineKey, string> = { eval: 'Eval bench', diagnose: 'Diagnose', plan: 'Plan desk', collect: 'Collect dock', review: 'Review gate', train: 'Training furnace', ship: 'Ship bay' };
  const lightsReadout = (l: Tdm115Lights | null) => (l ? `${l.passed}/${l.trials}` : '');
  const machines: Record<Tdm115MachineKey, Tdm115Machine> = {
    eval: { key: 'eval', label: label.eval, state: evalState, stationIndex: evalIdx, readout: [lightsReadout(baseline), lightsReadout(finetuned)].filter(Boolean) },
    diagnose: { key: 'diagnose', label: label.diagnose, state: diagState, stationIndex: 1, readout: clusters.length ? [plural(clusters.length, 'cause')] : [] },
    plan: { key: 'plan', label: label.plan, state: planState, stationIndex: 2, readout: p ? [tdm115Usd(p.estCostCents)] : [] },
    collect: { key: 'collect', label: label.collect, state: colState, stationIndex: 3, readout: dataTasks.length ? [`${done}/${dataTasks.length}`] : [] },
    review: { key: 'review', label: label.review, state: revState, stationIndex: 4, readout: reviews.length ? [`${accepted} pass`, `${rejected} reject`] : [] },
    train: { key: 'train', label: label.train, state: stateOf(5), stationIndex: 5, readout: [train.tag, train.step].filter(Boolean) as string[] },
    ship: { key: 'ship', label: label.ship, state: shipState, stationIndex: 7, readout: floor.stations[7]?.facts.slice(0, 1) ?? [] },
  };
  // A machine whose station went quiet stops glowing with its agent. Train and ship have no figure of their
  // own here, so they read the liveness directly.
  for (const a of agents) if (a.state === 'stalled' && a.kind !== 'droid' && machines[a.machine].state === 'working') machines[a.machine].state = 'stalled';
  for (const [k, i] of [['train', 5], ['ship', 7]] as const) if (lv?.[i]?.mode === 'stalled' && machines[k].state === 'working') machines[k].state = 'stalled';

  const parts: Tdm115Part[] = [
    { key: 'eval_runs', label: 'eval runs', count: evalRuns.length, between: ['eval', 'diagnose'] },
    { key: 'episodes', label: 'episodes', count: episodes.length, between: ['collect', 'review'] },
    { key: 'accepted', label: 'accepted', count: accepted, between: ['review', 'train'] },
    { key: 'checkpoints', label: 'checkpoints', count: checkpoints, between: ['train', 'eval'] },
  ];

  const active = floor.activeIndex >= 0 ? TDM115_MACHINE_OF_STATION[floor.activeIndex] ?? null : null;
  const activeAgent = agents.find((a) => a.machine === active && a.kind !== 'droid' && a.state !== 'idle' && a.state !== 'done');
  const readout = floor.ended
    ? floor.ended === 'shipped'
      ? 'Shipped'
      : floor.ended === 'failed'
        ? 'Ticket failed'
        : 'Cancelled'
    : floor.queued
      ? 'Queued'
      : activeAgent
        ? `${label[activeAgent.machine]}: ${TDM115_STATE_WORDS[activeAgent.state].toLowerCase()}`
        : active
          ? label[active]
          : '';

  return {
    ticketId: tid,
    robotId: robot ? Number(robot.id) : Number(ticket.robotId),
    robotName: robot?.name ?? `robot ${String(ticket.robotId)}`,
    sim,
    machines,
    agents,
    parts,
    lights: { baseline, finetuned },
    bins,
    plan,
    collect: { open, claimed, done, discord, text, drafted },
    review: { accepted, rejected, pending },
    train,
    ship,
    holder,
    waitingForRobot,
    activeIndex: floor.activeIndex,
    active,
    readout,
  };
}
