// TDM-99: the pure half of <RobotHud/> (components/robotview/Hud.tsx). No React, no SpacetimeDB:
// rows come in as plain objects so node:test can drive it (test/robotview-hud.test.ts).
//
// Every value here is derived from a real eval_run / eval_trial / training_run row or from the robot's
// /ws/state message. When the data is not there, the answer says so (null / 'unknown'), never a guess.

import type { RobotLiveState } from './types';

// ---------------------------------------------------------------- trial counter

/** The eval_run fields the counter reads (camelCase, as the generated bindings name them). */
export interface EvalRunLike {
  id: bigint | number;
  ticketId: bigint | number;
  robotId: bigint | number;
  trainingRunId: bigint | number;
  kind: string; // baseline | candidate | ab_live
  checkpointUri: string;
  status: string; // queued | running | done | failed | cancelled
  trials: number; // planned
  createdAt?: { microsSinceUnixEpoch: bigint } | null;
}

export interface EvalTrialLike {
  evalRunId: bigint | number;
  trialIndex: number;
  success: boolean;
  failureNote?: string | null;
}

export interface TrialCounter {
  runId: number;
  kind: string;
  status: string;
  running: boolean;
  planned: number;
  /** eval_trial rows recorded for the run. */
  done: number;
  passed: number;
  failed: number;
  /** 1-based trial in progress while running, else null. */
  current: number | null;
  /** One entry per planned trial, in order: what its eval_trial row says, or 'running' / 'pending'. */
  pips: ('pass' | 'fail' | 'running' | 'pending')[];
  /** e.g. "Trial 4/10" (running) or "8/10 passed" (finished). */
  headline: string;
  /** Any trial row tagged [FAKE] / [SIM] by the harness or the sim robot. */
  simulated: boolean;
}

const micros = (r: EvalRunLike) => (r.createdAt ? Number(r.createdAt.microsSinceUnixEpoch) : 0);

/**
 * The eval run the HUD should count: the newest RUNNING run, else the newest queued one, else the
 * newest finished one (so the counter stays up after the last trial). Optionally only for one ticket.
 */
export function pickEvalRun<R extends EvalRunLike>(runs: readonly R[], ticketId?: number | bigint | null): R | null {
  const pool = ticketId != null ? runs.filter((r) => BigInt(r.ticketId) === BigInt(ticketId)) : [...runs];
  if (!pool.length) return null;
  const newest = (a: R, b: R) => micros(b) - micros(a) || Number(BigInt(b.id) - BigInt(a.id));
  for (const status of ['running', 'queued']) {
    const hit = pool.filter((r) => r.status === status).sort(newest)[0];
    if (hit) return hit;
  }
  return pool.sort(newest)[0];
}

export function trialCounter(run: EvalRunLike, trials: readonly EvalTrialLike[]): TrialCounter {
  const mine = trials.filter((t) => BigInt(t.evalRunId) === BigInt(run.id));
  // one row per trial index (the reducer writes one; be robust to a replayed duplicate)
  const byIndex = new Map<number, EvalTrialLike>();
  for (const t of mine) byIndex.set(t.trialIndex, t);
  const rows = [...byIndex.values()];
  const passed = rows.filter((t) => t.success).length;
  const done = rows.length;
  const running = run.status === 'running';
  const planned = Math.max(run.trials, done);
  const nextIndex = (() => {
    for (let i = 0; i < planned; i++) if (!byIndex.has(i)) return i;
    return null;
  })();
  const current = running && nextIndex != null ? nextIndex + 1 : null;
  const pips: TrialCounter['pips'] = Array.from({ length: planned }, (_, i) => {
    const t = byIndex.get(i);
    if (t) return t.success ? 'pass' : 'fail';
    return running && i === nextIndex ? 'running' : 'pending';
  });
  const headline = current != null ? `Trial ${current}/${planned}` : running ? `${done}/${planned} trials` : `${passed}/${planned} passed`;
  const simulated = rows.some((t) => /^\[(FAKE|SIM)\]/.test(t.failureNote ?? ''));
  return { runId: Number(run.id), kind: run.kind, status: run.status, running, planned, done, passed, failed: done - passed, current, pips, headline, simulated };
}

// ---------------------------------------------------------------- checkpoint (base vs fine-tuned)

export const BASE_CHECKPOINT = 'gs://openpi-assets/checkpoints/pi05_base';
const BASE_ALIASES = new Set(['base', 'pi05_base', BASE_CHECKPOINT, `${BASE_CHECKPOINT}/params`]);

export interface TrainingRunLike {
  id: bigint | number;
  ticketId: bigint | number;
  iteration: number;
  status: string;
  baseCheckpointUri: string;
  checkpointUri?: string | null;
}

export interface CheckpointInfo {
  kind: 'base' | 'finetuned' | 'unknown' | 'none';
  /** Short name for the badge: "Base π0.5", "Fine-tuned, iteration 2". */
  label: string;
  /** The URI as reported, for a tooltip. */
  uri: string | null;
  /** Where the URI came from: the robot's own state, or the eval run row. */
  from: 'robot' | 'eval_run' | null;
  trainingRunId: number | null;
}

const norm = (u: string) => u.trim().replace(/\/+$/, '');

export function isBaseCheckpoint(uri: string): boolean {
  return BASE_ALIASES.has(norm(uri));
}

/**
 * Base vs fine-tuned from the checkpoint the robot reports (state.checkpoint_uri), else the eval run's
 * checkpoint_uri. Fine-tuned only when a training_run row produced exactly that URI; base when it is
 * the pi0.5 base checkpoint (or a training run's base_checkpoint_uri). Anything else is 'unknown',
 * shown with its URI rather than guessed.
 */
export function classifyCheckpoint(
  stateUri: string | null | undefined,
  run: Pick<EvalRunLike, 'checkpointUri' | 'trainingRunId' | 'kind'> | null,
  trainingRuns: readonly TrainingRunLike[],
): CheckpointInfo {
  const uri = stateUri?.trim() ? stateUri.trim() : run?.checkpointUri?.trim() ? run.checkpointUri.trim() : null;
  const from: CheckpointInfo['from'] = stateUri?.trim() ? 'robot' : uri ? 'eval_run' : null;
  if (!uri) return { kind: 'none', label: 'No checkpoint loaded', uri: null, from: null, trainingRunId: null };
  const tr = trainingRuns.find((t) => t.checkpointUri && norm(t.checkpointUri) === norm(uri));
  if (tr) return { kind: 'finetuned', label: `Fine-tuned, iteration ${tr.iteration}`, uri, from, trainingRunId: Number(tr.id) };
  if (isBaseCheckpoint(uri) || trainingRuns.some((t) => norm(t.baseCheckpointUri) === norm(uri)))
    return { kind: 'base', label: 'Base π0.5', uri, from, trainingRunId: null };
  // The eval run row says which training run it evaluates (0 = the baseline): trust it only for its own URI.
  if (run && norm(run.checkpointUri) === norm(uri) && BigInt(run.trainingRunId) === 0n && run.kind === 'baseline')
    return { kind: 'base', label: 'Base (baseline eval)', uri, from, trainingRunId: null };
  return { kind: 'unknown', label: 'Unrecognised checkpoint', uri, from, trainingRunId: null };
}

export function shortCheckpoint(uri: string, keep = 2): string {
  const parts = norm(uri).split('/');
  return parts.length <= keep ? uri : `…/${parts.slice(-keep).join('/')}`;
}

// ---------------------------------------------------------------- safety clamps

/** Human names for the safety layer's clamp counters (stream.py CLAMP_KEYS). */
export const CLAMP_LABEL: Record<string, string> = {
  clamped_joint_limit: 'joint limit',
  clamped_speed: 'speed',
  clamped_workspace: 'workspace',
  clamped_gripper: 'gripper',
  rejected_nonfinite: 'bad target',
  rejected_jump: 'jump',
  rejected_stopped: 'while stopped',
  watchdog_holds: 'watchdog hold',
  estops: 'e-stop',
};

/** Non-zero clamp counts of the last second, biggest first, as [label, count]. */
export function activeClamps(clamps: Record<string, number> | undefined | null): [string, number][] {
  if (!clamps) return [];
  return Object.entries(clamps)
    .filter(([, n]) => Number(n) > 0)
    .sort((a, b) => b[1] - a[1])
    .map(([k, n]) => [CLAMP_LABEL[k] ?? k.replace(/_/g, ' '), Number(n)]);
}

// ---------------------------------------------------------------- driver, gripper, plan

export const DRIVER_LABEL = { policy: 'POLICY', human: 'HUMAN', idle: 'IDLE' } as const;

export function gripperText(g: number): { word: 'Open' | 'Closed'; pct: number } {
  const pct = Math.round(Math.min(1, Math.max(0, g)) * 100);
  return { word: pct >= 50 ? 'Open' : 'Closed', pct };
}

export interface PlanSummary {
  points: number;
  /** Per joint (6), target minus current over the preview, rad: rows[k][j]. */
  deltas: number[][];
  /** Largest |delta| over the plan, rad. */
  maxDelta: number;
  /** Gripper at the end of the plan, 0..1, when the rows carry it. */
  gripperEnd: number | null;
}

/** The policy's chunk preview relative to where the arm is now (null when there is no plan). */
export function planSummary(state: Pick<RobotLiveState, 'joints' | 'chunk_preview'> | null | undefined): PlanSummary | null {
  const rows = state?.chunk_preview;
  if (!rows || rows.length < 2 || !state?.joints || state.joints.length < 6) return null;
  const q = state.joints;
  const good = rows.filter((r) => r.length >= 6 && r.slice(0, 6).every(Number.isFinite));
  if (good.length < 2) return null;
  const deltas = good.map((r) => r.slice(0, 6).map((v, j) => v - q[j]));
  const maxDelta = Math.max(...deltas.flat().map(Math.abs));
  const last = good[good.length - 1];
  return { points: good.length, deltas, maxDelta, gripperEnd: last.length > 6 && Number.isFinite(last[6]) ? last[6] : null };
}
