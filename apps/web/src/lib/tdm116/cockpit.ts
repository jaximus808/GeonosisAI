// TDM-116: the ticket cockpit's pure half. No React, no side-effect imports, so
// `pnpm --filter @rdf/web test` (test/tdm116-cockpit.test.ts) runs it under plain Node.
//
// Which station the main stage shows (it follows the ticket on its own), what the one primary action
// is right now, and the small derived numbers each stage view needs (ETA, drops by channel). Every
// value comes from rows or event_log; nothing is invented.

export type Tdm116StageKey = 'baseline' | 'diagnose' | 'plan_data' | 'collect' | 'review' | 'train' | 'eval' | 'ship';

export const TDM116_STAGE_KEYS: readonly Tdm116StageKey[] = ['baseline', 'diagnose', 'plan_data', 'collect', 'review', 'train', 'eval', 'ship'];

export const TDM116_TABS = [
  { key: 'overview', label: 'Overview', short: 'Stage' },
  { key: 'robot', label: 'Robot', short: 'Robot' },
  { key: 'data', label: 'Data', short: 'Data' },
  { key: 'money', label: 'Money', short: 'Money' },
  { key: 'agents', label: 'Agents', short: 'Agents' },
  { key: 'events', label: 'Events', short: 'Events' },
  { key: 'settings', label: 'Settings', short: 'Settings' },
  { key: 'report', label: 'Report', short: 'Report' },
] as const;
export type Tdm116TabKey = (typeof TDM116_TABS)[number]['key'];

type TsLike = { microsSinceUnixEpoch: bigint };
const us = (t: TsLike | null | undefined) => (t ? Number(t.microsSinceUnixEpoch) : 0);

/** The floor fields this module reads (structural: lib/tdm112/floor.ts Tdm112Floor satisfies it). */
export interface Tdm116FloorLike {
  activeIndex: number;
  ended?: 'shipped' | 'failed' | 'cancelled';
  queued: boolean;
  stations: readonly { key: Tdm116StageKey; state: string }[];
}

/**
 * The station the ticket is at now: the running (or waiting) one; Ship once shipped; the station it
 * stopped at when failed or cancelled; the baseline while queued.
 */
export function tdm116LiveStage(floor: Tdm116FloorLike | null | undefined, stoppedAt?: Tdm116StageKey | null): Tdm116StageKey {
  if (!floor) return 'baseline';
  if (floor.activeIndex >= 0 && floor.stations[floor.activeIndex]) return floor.stations[floor.activeIndex].key;
  if (floor.ended === 'shipped') return 'ship';
  const failed = floor.stations.find((s) => s.state === 'failed');
  if (failed) return failed.key;
  // Cancelled: the station it was at when cancelled (`stoppedAt`, from the status change), else the last
  // station that finished, else the baseline.
  if (floor.ended && stoppedAt) return stoppedAt;
  const done = floor.stations.filter((s) => s.state === 'done');
  if (floor.ended && done.length) return done[done.length - 1].key;
  return 'baseline';
}

/** ticket.status -> the station that runs it (the floor's mapping: dispatch is Collect, shipped is Ship). */
const TDM116_STATUS_STAGE: Record<string, Tdm116StageKey> = {
  baseline: 'baseline', diagnose: 'diagnose', plan_data: 'plan_data', dispatch: 'collect', review: 'review', train: 'train', eval: 'eval', shipped: 'ship',
};

/** Where a failed or cancelled ticket stopped: the `from` of its last ticket.status_changed into that status. */
export function tdm116StoppedAt(events: readonly Tdm116EventLike[], status: string): Tdm116StageKey | null {
  if (status !== 'failed' && status !== 'cancelled') return null;
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.type === 'ticket.status_changed' && e.payload.to === status) return TDM116_STATUS_STAGE[String(e.payload.from)] ?? null;
  }
  return null;
}

/** `?stage=` -> a stage key (pinned view), or null. */
export function tdm116ParseStage(v: string | null | undefined): Tdm116StageKey | null {
  return v && (TDM116_STAGE_KEYS as readonly string[]).includes(v) ? (v as Tdm116StageKey) : null;
}

/** `?tab=` -> a tab key, default overview. */
export function tdm116ParseTab(v: string | null | undefined): Tdm116TabKey {
  return TDM116_TABS.some((t) => t.key === v) ? (v as Tdm116TabKey) : 'overview';
}

export const TDM116_STAGE_LABEL: Record<Tdm116StageKey, string> = {
  baseline: 'Baseline',
  diagnose: 'Diagnose',
  plan_data: 'Plan data',
  collect: 'Collect',
  review: 'Review',
  train: 'Train',
  eval: 'Eval',
  ship: 'Ship',
};

// ------------------------------------------------------------------------------ primary action

export interface Tdm116PlanLike { id: bigint; iteration: number; status: string; estCostCents: bigint; createdAt: TsLike }
export interface Tdm116TaskLike { id: bigint; type: string; status: string; createdAt: TsLike }
export interface Tdm116EvalRunLike { id: bigint; kind: string; status: string; trials: number; successes: number; ciLow: number; createdAt: TsLike }

export type Tdm116Action =
  | { kind: 'approve'; label: string; planId: number; cents: number }
  | { kind: 'takeover'; label: string; taskId: number; href: string }
  | { kind: 'report'; label: string; href: string }
  | null;

const usd = (cents: number) => `$${(cents / 100).toFixed(2)}`;
const newest = <T extends { createdAt: TsLike }>(rows: readonly T[]) => [...rows].sort((a, b) => us(b.createdAt) - us(a.createdAt))[0];

/**
 * The one thing a person can do to move the ticket right now, if there is one:
 *   plan_data with a proposed plan  -> "Approve plan $17.50"
 *   collect with an open takeover   -> "Take over #12" (a paused rollout waits for a contributor)
 *   shipped                         -> "View report"
 * Nothing else: the line runs on its own.
 */
export function tdm116PrimaryAction(input: {
  ticketId: number | bigint;
  status: string;
  iteration: number;
  plans: readonly Tdm116PlanLike[];
  tasks: readonly Tdm116TaskLike[];
}): Tdm116Action {
  const { status } = input;
  if (status === 'plan_data') {
    const plan = newest(input.plans.filter((p) => p.iteration === input.iteration)) ?? newest(input.plans);
    if (plan && plan.status === 'proposed') {
      const cents = Number(plan.estCostCents);
      return { kind: 'approve', label: `Approve plan ${usd(cents)}`, planId: Number(plan.id), cents };
    }
  }
  if (status === 'dispatch') {
    const open = [...input.tasks]
      .filter((t) => t.status === 'open' && t.type === 'recovery_takeover')
      .sort((a, b) => us(a.createdAt) - us(b.createdAt))[0];
    if (open) return { kind: 'takeover', label: `Take over #${open.id}`, taskId: Number(open.id), href: `/marketplace?task=${open.id}` };
  }
  if (status === 'shipped') return { kind: 'report', label: 'View report', href: `/tickets/${input.ticketId}/report` };
  return null;
}

// ------------------------------------------------------------------------------ eval results
// (A running eval's trial x/N, phase and ETA come from TDM-114's floor.evalPhase.)

export interface Tdm116TrialLike { trialIndex: number; success: boolean; durationMs: number; failureNote?: string | null; createdAt: TsLike }

/** The newest failed trial's note, harness tags stripped. */
export function tdm116LastFailure(trials: readonly Tdm116TrialLike[]): { index: number; note: string } | null {
  const failed = [...trials].filter((t) => !t.success).sort((a, b) => us(b.createdAt) - us(a.createdAt) || b.trialIndex - a.trialIndex)[0];
  if (!failed) return null;
  const note = (failed.failureNote ?? '').replace(/^\s*\[(SIM|FAKE|SEEDED)\]\s*/i, '').replace(/^Auto outcome from sim state:\s*/i, '').trim();
  return { index: failed.trialIndex, note: note || 'No note recorded' };
}

/** "3m 20s", "45s", "1h 02m". */
export function tdm116Duration(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, '0')}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`;
}

// ------------------------------------------------------------------------------ drops by channel

export interface Tdm116EventLike { type: string; payload: Record<string, unknown> }

export interface Tdm116ChannelRow { channel: string; posted: number; drafted: number; claims: number }

const CHANNEL_NAME: Record<string, string> = { discord: 'Discord', text: 'Text', qr: 'QR', web: 'Web', sms: 'Text', imessage: 'Text' };
export const tdm116ChannelName = (c: string) => CHANNEL_NAME[c.toLowerCase()] ?? (c ? c[0].toUpperCase() + c.slice(1) : 'Other');

/**
 * Job drops and claims per channel for this ticket's data tasks: job_drop.posted {channel},
 * job_drop.preview (drafted; posted only in live mode), job_drop.claimed {src} (carries no ticket id, so
 * pass every event_log row), text.offer_sent (TDM-113 text offers). A text claim is logged both as
 * text.claimed and job_drop.claimed {src: text}; only the latter is counted. Task ids outside `taskIds`
 * (reset tasks, other tickets) are skipped.
 */
export function tdm116Drops(events: readonly Tdm116EventLike[], taskIds: ReadonlySet<number>): Tdm116ChannelRow[] {
  const rows = new Map<string, Tdm116ChannelRow>();
  const row = (c: string) => {
    const k = tdm116ChannelName(c);
    const r = rows.get(k) ?? { channel: k, posted: 0, drafted: 0, claims: 0 };
    rows.set(k, r);
    return r;
  };
  const mine = (p: Record<string, unknown>) => {
    const ids = Array.isArray(p.task_ids) ? (p.task_ids as unknown[]) : [p.task_id];
    return ids.some((x) => taskIds.has(Number(x)));
  };
  for (const e of events) {
    const p = e.payload;
    if (!mine(p)) continue;
    const ch = String(p.channel ?? p.src ?? '');
    if (e.type === 'job_drop.posted') row(ch || 'other').posted += 1;
    else if (e.type === 'job_drop.preview') {
      // {channels: {discord: "drafted", ...}} (a list in older rows)
      const raw = p.channels;
      const chans = Array.isArray(raw) ? raw.map(String) : raw && typeof raw === 'object' ? Object.keys(raw) : [ch || 'discord'];
      for (const c of chans) row(c).drafted += 1;
    } else if (e.type === 'job_drop.claimed') row(String(p.src || 'web')).claims += 1;
    else if (e.type === 'text.offer_sent') {
      // TDM-113: one event per drop; `count` texts went out (dry_run: written to the outbox, not sent).
      const k = Number(p.count) || 0;
      if (p.mode === 'live') row('text').posted += Math.max(1, k);
      else row('text').drafted += Math.max(1, k);
    }
  }
  return [...rows.values()].sort((a, b) => b.claims - a.claims || b.posted - a.posted || a.channel.localeCompare(b.channel));
}

// ------------------------------------------------------------------------------ cancel

/** Terminal statuses: nothing left to cancel. */
export const TDM116_TERMINAL = new Set(['shipped', 'failed', 'cancelled']);

/** The note written with a web cancel (the reducer stores it on the status change). */
export function tdm116CancelNote(reason: string, by = 'web-operator'): string {
  const r = reason.replace(/\s+/g, ' ').trim().slice(0, 300);
  return r ? `Cancelled by ${by}: ${r}` : `Cancelled by ${by}`;
}

// ------------------------------------------------------------------------------ dashboard view prefs
// The /dashboard ticket list (collapse to a rail, "Hide cancelled") and the Overview's Stage | Factory
// switch. Stored per viewer in localStorage; these parse what comes back (anything else -> the default).

export const TDM116_PREF_KEYS = {
  ticketsCollapsed: 'gx.dash.ticketsCollapsed',
  hideCancelled: 'gx.dash.hideCancelled',
  overviewView: 'gx.cockpit.overviewView',
} as const;

export type Tdm116OverviewView = 'stage' | 'factory';

/** A stored '1'/'0' -> boolean; missing or junk -> `fallback`. */
export function tdm116ParseFlag(v: string | null | undefined, fallback: boolean): boolean {
  return v === '1' ? true : v === '0' ? false : fallback;
}

/** A stored overview view -> 'stage' | 'factory' (default stage). */
export function tdm116ParseOverviewView(v: string | null | undefined): Tdm116OverviewView {
  return v === 'factory' ? 'factory' : 'stage';
}

/** Clutter in the ticket list: cancelled tickets, failed tickets (except the labelled [Sample] example), and
 * test tickets (task text starting `test.`). */
export function tdm116IsClutter(t: { status: string; taskDescription: string }): boolean {
  if (/^\s*\[Sample\]/i.test(t.taskDescription)) return false;
  return t.status === 'cancelled' || t.status === 'failed' || /^\s*test\./i.test(t.taskDescription);
}

/**
 * The tickets the list shows. With `hide` on, clutter is dropped, except the selected ticket (the one in
 * the cockpit stays listed so the viewer can see where they are). Order is kept.
 */
export function tdm116ListTickets<T extends { id: bigint; status: string; taskDescription: string }>(
  tickets: readonly T[],
  hide: boolean,
  selected: bigint | null,
): { shown: T[]; hidden: number } {
  if (!hide) return { shown: [...tickets], hidden: 0 };
  const shown = tickets.filter((t) => t.id === selected || !tdm116IsClutter(t));
  return { shown, hidden: tickets.length - shown.length };
}

/** The `[` shortcut toggles the ticket list, but not while typing or with a modifier held. */
export function tdm116IsListToggleKey(e: { key: string; metaKey?: boolean; ctrlKey?: boolean; altKey?: boolean; target?: unknown }): boolean {
  if (e.key !== '[' || e.metaKey || e.ctrlKey || e.altKey) return false;
  const el = e.target as { tagName?: string; isContentEditable?: boolean } | null | undefined;
  const tag = (el?.tagName ?? '').toUpperCase();
  return !(tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el?.isContentEditable);
}

/** The full ticket cockpit page. A plain click on a /dashboard ticket (list item or the embedded title) opens it. */
export function tdm116TicketHref(id: bigint | number | string): string {
  return `/tickets/${id.toString()}`;
}

/** Keep the ticket inside /dashboard (the "Preview here" link). */
export function tdm116PreviewHref(id: bigint | number | string): string {
  return `/dashboard?ticket=${id.toString()}`;
}
