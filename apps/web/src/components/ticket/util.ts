// TDM-75 ticket page helpers. Pure functions only (no hooks), shared by the ticket/* components.
import type { Timestamp } from 'spacetimedb';
import type { RowOf } from '@/lib/spacetime/hooks';

export type Ticket = RowOf<'ticket'>;
export type Robot = RowOf<'robot'>;
export type EvalRun = RowOf<'evalRun'>;
export type EvalTrial = RowOf<'evalTrial'>;
export type Episode = RowOf<'episode'>;
export type Review = RowOf<'review'>;
export type Task = RowOf<'task'>;
export type Payment = RowOf<'payment'>;
export type TrainingRun = RowOf<'trainingRun'>;
export type DataPlan = RowOf<'dataPlan'>;
export type FailureCluster = RowOf<'failureCluster'>;
export type Contributor = RowOf<'contributor'>;
export type DatasetOffer = RowOf<'datasetOffer'>;
export type EventRow = RowOf<'eventLog'>;

export type Json = Record<string, unknown>;

export function parseObj(text: string | undefined | null): Json | null {
  if (!text) return null;
  try {
    const v = JSON.parse(text);
    return v && typeof v === 'object' && !Array.isArray(v) ? (v as Json) : null;
  } catch {
    return null;
  }
}

/** Newest first by created_at (ids are not ordered, see spacetime/CLAUDE.md). */
export function byNewest<T extends { createdAt: Timestamp }>(rows: readonly T[]): T[] {
  return [...rows].sort((a, b) => Number(b.createdAt.microsSinceUnixEpoch - a.createdAt.microsSinceUnixEpoch));
}
export function byOldest<T extends { createdAt: Timestamp }>(rows: readonly T[]): T[] {
  return byNewest(rows).reverse();
}

/** Same rounding as stats.py _pct (round half up). */
export function pct(x: number): number {
  return Math.floor(x * 100 + 0.5);
}

/** stats.py format_ci, built from the stored row values (used only if the finalize event is missing). */
export function ciText(successes: number, trials: number, lo: number, hi: number): string {
  return `${successes}/${trials}, 95% CI ${pct(lo)}–${pct(hi)}%`;
}

export function shortUri(uri: string | undefined, keep = 3): string {
  if (!uri) return '';
  const parts = uri.split('/').filter(Boolean);
  return parts.length <= keep + 1 ? uri : `…/${parts.slice(-keep).join('/')}`;
}

export function fmtDuration(ms: number): string {
  if (ms < 1000) return `${ms} ms`;
  const s = ms / 1000;
  return s < 60 ? `${s.toFixed(1)} s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
}

export function fmtTime(ts: Timestamp | undefined): string {
  if (!ts) return '';
  return ts.toDate().toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}

// ---------------------------------------------------------------- fake-robot harness tags
// services/robot-client/src/rdf_robot_client/fake.py marks everything it writes:
// robot embodiment fake_yam / name fake-*, cameras_json {"fake": true}, storage_uri .../FAKE-ep-*,
// eval_trial.failure_note "[FAKE] ...", event payloads {"fake": true}.

export function isFakeRobot(r: Robot | undefined): boolean {
  if (!r) return false;
  return (
    r.embodiment.toLowerCase().startsWith('fake') ||
    r.name.toLowerCase().startsWith('fake') ||
    r.adapterVersion.toLowerCase().startsWith('fake') ||
    parseObj(r.cameraConfigJson)?.fake === true
  );
}

export function isFakeEpisode(e: Episode): boolean {
  return parseObj(e.camerasJson)?.fake === true || /FAKE-/.test(e.storageUri);
}

/** RDF_SEED_EVALS demo run-through: the outcome was drawn, not run on the robot. */
export function isSeededTrial(t: EvalTrial): boolean {
  return (t.failureNote ?? '').startsWith('[SEEDED]');
}

/** RDF_MODAL_STUB training: no GPU ran and the checkpoint is fake ("[STUB] no GPU"). */
export function isStubCheckpoint(uri: string | null | undefined): boolean {
  return !!uri && uri.includes('stub-no-gpu');
}

export function isFakeTrial(t: EvalTrial): boolean {
  return (t.failureNote ?? '').startsWith('[FAKE]');
}

// ---------------------------------------------------------------- cameras

/** cameras_json is {name: uri} or {name: {uri}}; the fake harness nests it under "cameras". */
export function cameraUris(e: Episode): { name: string; uri: string }[] {
  const raw = parseObj(e.camerasJson);
  if (!raw) return [];
  const src = raw.cameras && typeof raw.cameras === 'object' ? (raw.cameras as Json) : raw;
  const out: { name: string; uri: string }[] = [];
  for (const [name, v] of Object.entries(src)) {
    if (name === 'fake' || name === 'tag') continue;
    const uri = typeof v === 'string' ? v : v && typeof v === 'object' ? (v as Json).uri : undefined;
    if (typeof uri === 'string' && uri) out.push({ name, uri });
  }
  return out.sort((a, b) => a.name.localeCompare(b.name));
}

// ---------------------------------------------------------------- flywheel stages

export const STAGES = [
  { key: 'created', label: 'Created' },
  { key: 'onboarding', label: 'Onboard' },
  { key: 'baseline', label: 'Baseline' },
  { key: 'diagnose', label: 'Diagnose' },
  { key: 'plan_data', label: 'Plan data' },
  { key: 'dispatch', label: 'Collect' },
  { key: 'review', label: 'Review' },
  { key: 'train', label: 'Train' },
  { key: 'eval', label: 'Evaluate' },
  { key: 'shipped', label: 'Shipped' },
] as const;

export function stageLabel(status: string): string {
  return STAGES.find((s) => s.key === status)?.label ?? status.replace(/_/g, ' ');
}

export const DATA_TYPE_LABEL: Record<string, string> = {
  recovery_takeover: 'Recovery takeovers',
  clean_teleop: 'Clean teleop demos',
  purchased: 'Purchased episodes',
  reset: 'Resets',
};

export const TASK_TYPE_LABEL: Record<string, string> = {
  recovery_takeover: 'Recovery takeover',
  clean_teleop: 'Clean teleop demo',
  reset: 'Reset',
};

export const KIND_LABEL: Record<string, string> = {
  baseline: 'Baseline',
  eval: 'Eval',
  clean_teleop: 'Clean teleop',
  recovery_takeover: 'Takeover',
  purchased: 'Purchased',
};

/** Ticket-scoped events, parsed once. Events carry ticket_id as a JSON number. */
export type TicketEvent = { row: EventRow; payload: Json };
export function ticketEvents(rows: readonly EventRow[], ticketId: bigint): TicketEvent[] {
  const id = Number(ticketId);
  const out: TicketEvent[] = [];
  for (const row of rows) {
    if (!row.payloadJson.includes('ticket_id')) continue;
    const payload = parseObj(row.payloadJson);
    if (payload && Number(payload.ticket_id) === id) out.push({ row, payload });
  }
  return out.sort((a, b) => Number(a.row.ts.microsSinceUnixEpoch - b.row.ts.microsSinceUnixEpoch));
}
