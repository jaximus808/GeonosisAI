// TDM-101: the pure half of the /live projector page. No React, no imports with side effects, so
// `pnpm --filter @rdf/web test` (test/tdm101-live.test.ts) runs it under plain Node.

export type TsLike = { microsSinceUnixEpoch: bigint };
const us = (t: TsLike | undefined | null) => (t ? Number(t.microsSinceUnixEpoch) : 0);

// ------------------------------------------------------------------------------ which ticket

export const TDM101_ENDED = new Set(['shipped', 'failed', 'cancelled']);

export interface Tdm101TicketLike {
  id: bigint;
  status: string;
  createdAt: TsLike;
  createdBy?: string;
}

/**
 * The ticket the kiosk shows. `?ticket=N` pins it (even before the row exists). Otherwise the newest
 * ticket still in flight; when none is, the newest ticket of any status (so a ticket that just shipped
 * stays on screen with its result instead of the page going blank). Test tickets (`test.*` creators)
 * are skipped unless nothing else exists.
 */
export function tdm101PickTicket<T extends Tdm101TicketLike>(
  tickets: readonly T[],
  pinned: bigint | null,
): { ticket: T | null; how: 'pinned' | 'active' | 'latest' | 'none' } {
  if (pinned !== null) return { ticket: tickets.find((t) => t.id === pinned) ?? null, how: 'pinned' };
  const real = tickets.filter((t) => !(t.createdBy ?? '').startsWith('test.'));
  const pool = real.length ? real : tickets;
  const newest = [...pool].sort((a, b) => us(b.createdAt) - us(a.createdAt) || Number(b.id - a.id));
  const active = newest.find((t) => !TDM101_ENDED.has(t.status));
  if (active) return { ticket: active, how: 'active' };
  return newest[0] ? { ticket: newest[0], how: 'latest' } : { ticket: null, how: 'none' };
}

/** `?ticket=` value -> id, or null when absent or not a positive integer. */
export function tdm101ParseTicketParam(v: string | string[] | undefined | null): bigint | null {
  const s = Array.isArray(v) ? v[0] : v;
  if (!s || !/^\d+$/.test(s.trim())) return null;
  const n = BigInt(s.trim());
  return n > 0n ? n : null;
}

// ------------------------------------------------------------------------------ planner decision

/**
 * The planner's reasoning cut to its first sentence, verbatim (no rewording), at most `max`
 * characters with an ellipsis when cut mid-sentence.
 */
export function tdm101OneLine(text: string, max = 160): string {
  const flat = text.replace(/\s+/g, ' ').trim();
  if (!flat) return '';
  const m = /^(.+?[.!?])(\s|$)/.exec(flat);
  const first = m ? m[1] : flat;
  if (Array.from(first).length <= max) return first;
  const cut = Array.from(first).slice(0, max - 1).join('');
  const atWord = cut.replace(/\s+\S*$/, '');
  return `${atWord.length > max * 0.6 ? atWord : cut}…`;
}

// ------------------------------------------------------------------------------ eval pair

export interface Tdm101EvalLike {
  id: bigint;
  kind: string;
  status: string;
  trials: number;
  completed: number;
  successes: number;
  ciLow: number;
  ciHigh: number;
  createdAt: TsLike;
}

export interface Tdm101Rate {
  run: Tdm101EvalLike;
  /** Observed rate over the trials finished so far (final when done). Null before the first trial. */
  rate: number | null;
  /** Wilson 95% CI as stored by finalize_eval_run; only once the run is done. */
  ci: { lo: number; hi: number } | null;
  done: boolean;
  /** Trials counted in `rate`. */
  n: number;
}

function rateOf(run: Tdm101EvalLike): Tdm101Rate {
  const done = run.status === 'done' && run.trials > 0;
  const n = done ? run.trials : run.completed;
  return {
    run,
    rate: n > 0 ? run.successes / n : null,
    ci: done ? { lo: run.ciLow, hi: run.ciHigh } : null,
    done,
    n,
  };
}

/** Newest baseline run vs the newest non-baseline run (the fine-tuned candidate, or a live A/B). */
export function tdm101EvalPair<R extends Tdm101EvalLike>(runs: readonly R[]): { baseline: Tdm101Rate | null; current: Tdm101Rate | null } {
  const newest = [...runs].sort((a, b) => us(b.createdAt) - us(a.createdAt));
  const b = newest.find((r) => r.kind === 'baseline');
  const c = newest.find((r) => r.kind !== 'baseline');
  return { baseline: b ? rateOf(b) : null, current: c ? rateOf(c) : null };
}

// ------------------------------------------------------------------------------ agents ticker

/** Structural slice of TDM-35's AgentFeed (lib/agentfeed/normalize.ts). */
export interface Tdm101FeedEvent {
  id: string;
  ts_us: number;
  kind: string;
  type: string;
  actor: string;
  text: string;
  is_error: boolean;
  data: Record<string, unknown>;
}
export interface Tdm101FeedAgent {
  key: string;
  label: string;
  role: string;
  kind: string;
  events: Tdm101FeedEvent[];
}

export interface Tdm101TickerItem {
  id: string;
  tsUs: number;
  agent: string;
  role: string;
  text: string;
  kind: string;
  isError: boolean;
  /** The resume bridge (TDM-27) woke this agent because a SpacetimeDB row made its predicate true. */
  wake: { predicate: string } | null;
}

/** A droid's TDM-35 label is '<name> (<role words> #<id>)'; the ticker shows the name only. */
function shortLabel(label: string): string {
  return label.replace(/\s*\([^()]*#\d+\)$/, '') || label;
}

/**
 * Same test TDM-76 uses (lib/tdm76/feed.ts asResume): an agent.resumed row whose reason the resume
 * bridge wrote as "predicate true: <predicate>; <what changed>; ...". Manual resumes and recordings
 * don't claim a database condition, so they are not highlighted.
 */
export function tdm101Wake(e: Pick<Tdm101FeedEvent, 'type' | 'data'>): { predicate: string } | null {
  if (e.type !== 'agent.resumed') return null;
  const reason = String(e.data?.reason ?? '');
  if (!reason.startsWith('predicate true:')) return null;
  const predicate = reason.slice('predicate true:'.length).split(';')[0].trim();
  return { predicate };
}

/** Every agent's events, newest first, deduplicated, at most `limit`. */
export function tdm101Ticker(agents: readonly Tdm101FeedAgent[], limit = 14): Tdm101TickerItem[] {
  const seen = new Set<string>();
  const out: Tdm101TickerItem[] = [];
  for (const a of agents) {
    for (const e of a.events) {
      if (seen.has(e.id)) continue;
      seen.add(e.id);
      out.push({
        id: e.id,
        tsUs: e.ts_us,
        agent: shortLabel(a.label),
        role: a.role,
        text: e.text,
        kind: e.kind,
        isError: e.is_error,
        wake: tdm101Wake(e),
      });
    }
  }
  out.sort((x, y) => y.tsUs - x.tsUs || (x.id < y.id ? 1 : -1));
  return out.slice(0, limit);
}

// ------------------------------------------------------------------------------ keyboard

export type Tdm101KeyAction =
  | { kind: 'mode'; mode: 'sim' | 'live' }
  | { kind: 'fullscreen' }
  | { kind: 'preset'; index: 0 | 1 | 2 };

/** S / L switch the robot view, F toggles full screen, 1-3 pick the 3D camera preset. */
export function tdm101KeyAction(e: { key: string; ctrlKey?: boolean; metaKey?: boolean; altKey?: boolean }): Tdm101KeyAction | null {
  if (e.ctrlKey || e.metaKey || e.altKey) return null;
  switch (e.key.toLowerCase()) {
    case 's':
      return { kind: 'mode', mode: 'sim' };
    case 'l':
      return { kind: 'mode', mode: 'live' };
    case 'f':
      return { kind: 'fullscreen' };
    case '1':
      return { kind: 'preset', index: 0 };
    case '2':
      return { kind: 'preset', index: 1 };
    case '3':
      return { kind: 'preset', index: 2 };
    default:
      return null;
  }
}

// ------------------------------------------------------------------------------ the line (brand)

/** GeonosisAI stations, in order, and the ticket.status values each one covers (docs/brand/project/README.md). */
export const TDM101_STATIONS = [
  { key: 'queued', label: 'Queued', statuses: ['created', 'onboarding'] },
  { key: 'baseline', label: 'Baseline', statuses: ['baseline'] },
  { key: 'diagnose', label: 'Diagnose', statuses: ['diagnose'] },
  { key: 'plan_data', label: 'Plan data', statuses: ['plan_data'] },
  { key: 'collect', label: 'Collect', statuses: ['dispatch'] },
  { key: 'review', label: 'Review', statuses: ['review'] },
  { key: 'train', label: 'Train', statuses: ['train'] },
  { key: 'eval', label: 'Eval', statuses: ['eval'] },
  { key: 'shipped', label: 'Shipped', statuses: ['shipped'] },
] as const;

export type Tdm101StationKey = (typeof TDM101_STATIONS)[number]['key'];
export type Tdm101StationState = 'done' | 'current' | 'upcoming' | 'failed' | 'cancelled' | 'shipped';

export function tdm101StationOf(status: string): number {
  return TDM101_STATIONS.findIndex((s) => (s.statuses as readonly string[]).includes(status));
}

/**
 * Each station's state. The running station is `current` (the one melt rivet). An eval that loops
 * the ticket back resets every later station to upcoming (one line, never a second one). A failed or
 * cancelled ticket marks the station it stopped at (`stoppedAt` = the status it left, from the
 * ticket.status_changed event) and leaves the rest upcoming.
 */
export function tdm101LineStates(status: string, stoppedAt?: string | null): Tdm101StationState[] {
  const ended = status === 'failed' || status === 'cancelled';
  const at = ended ? tdm101StationOf(stoppedAt ?? '') : tdm101StationOf(status);
  return TDM101_STATIONS.map((s, i) => {
    if (at < 0) return 'upcoming';
    if (i < at) return 'done';
    if (i > at) return 'upcoming';
    if (ended) return status === 'failed' ? 'failed' : 'cancelled';
    return s.key === 'shipped' ? 'shipped' : 'current';
  });
}
