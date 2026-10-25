// AgentWorkbench: what the agent at one station is doing right now, and whether it is really doing it.
// Pure (no React, no side-effect imports), so `pnpm --filter @rdf/web test` (test/agentwork.test.ts) runs it
// under plain Node.
//
// In: one TDM-112 station view, that ticket's event_log rows (oldest first) and "now".
// Out: the droid's mode (working, thinking, waiting, stalled, done, failed, idle), a heartbeat (the last
// activity and how stale it is), and a Claude-Code-style transcript of the station's latest run.
//
// The same liveness (`awLiveness`) drives the cockpit's workbench droid and the FactoryIso droids on
// /factory and in the ticket view, so a figure only animates while its station really shows activity.
//
// Lines come from `agent.step` / `agent.thought` rows the orchestrator writes (rdf_common.llm.agent_event:
// LLM start, heartbeat, the model's reasoning; station steps like "Trial 4/10: fail") and from the
// station's ordinary event_log rows (eval.trial, planner.data_plan, ...) in TDM-112's plain words. When
// narration covers an ordinary row (both say "trial 4 failed") only the narration is shown.

import type { Tdm112Event, Tdm112StationKey, Tdm112StationView } from '../tdm112/floor';

/** Plain words for an ordinary event_log row (callers pass TDM-112's tdm112Words; kept a parameter so this
 * module has no value imports and runs under plain Node). */
export type AwWords = (e: Pick<Tdm112Event, 'type' | 'payload'>) => string;
const fallbackWords: AwWords = (e) => e.type.replace(/[._]/g, ' ');

export type AwMode = 'working' | 'thinking' | 'waiting' | 'stalled' | 'done' | 'failed' | 'idle';
export type AwFreshness = 'fresh' | 'slow' | 'stale' | 'none';
/** ● step, ✻ thought, → decision, ✗ error, ⏸ waiting, · an ordinary event row. */
export type AwGlyph = 'step' | 'thought' | 'decision' | 'error' | 'wait' | 'event';

export const AW_GLYPH: Record<AwGlyph, string> = { step: '●', thought: '✻', decision: '→', error: '✗', wait: '⏸', event: '·' };

export interface AwLine {
  id: string;
  glyph: AwGlyph;
  text: string;
  tsUs: number;
  /** The newest line while the agent works: it carries the spinner. */
  live?: boolean;
}

export interface AwAgent { key: string; name: string; kind: 'claude' | 'service' | 'droid' }

/** Who works each station (keys match the orchestrator's agent_steps.AGENT_OF and FactoryIso's figures). */
export const AW_AGENT: Record<Tdm112StationKey, AwAgent> = {
  baseline: { key: 'eval_runner', name: 'Eval runner', kind: 'service' },
  diagnose: { key: 'clusterer', name: 'Failure clusterer', kind: 'claude' },
  plan_data: { key: 'planner', name: 'Planner', kind: 'claude' },
  collect: { key: 'dispatcher', name: 'Dispatcher', kind: 'service' },
  review: { key: 'reviewer', name: 'Reviewer', kind: 'claude' },
  train: { key: 'trainer', name: 'Trainer', kind: 'service' },
  eval: { key: 'eval_runner', name: 'Eval runner', kind: 'service' },
  ship: { key: 'flywheel', name: 'Flywheel', kind: 'service' },
};

/** Seconds of silence before the heartbeat turns amber (slow) and red (stale), per station. LLM stations
 * get a heartbeat row every 10 s while Claude works, so 30 s of silence there already means something. */
export const AW_LIMITS: Record<Tdm112StationKey, { slowS: number; staleS: number }> = {
  baseline: { slowS: 90, staleS: 300 },
  diagnose: { slowS: 30, staleS: 120 },
  plan_data: { slowS: 30, staleS: 120 },
  collect: { slowS: 180, staleS: 900 },
  review: { slowS: 60, staleS: 300 },
  train: { slowS: 60, staleS: 240 },
  eval: { slowS: 90, staleS: 300 },
  ship: { slowS: 30, staleS: 120 },
};

/** Stations where a long silence means "waiting on people", not "stuck" (nothing to do until a row lands). */
const QUIET_MEANS: Partial<Record<Tdm112StationKey, string>> = {
  collect: 'Waiting for contributors to record',
  review: 'Waiting for recorded episodes',
};

// Ordinary rows a narration line already says, per station.
const COVERED: Partial<Record<Tdm112StationKey, readonly string[]>> = {
  baseline: ['eval.trial', 'eval.trial_recorded', 'eval.finalized'],
  eval: ['eval.trial', 'eval.trial_recorded', 'eval.finalized'],
  diagnose: ['diagnose.start', 'diagnose.started', 'diagnose.clusters', 'diagnose.no_failures', 'planner.failure_cluster'],
  plan_data: ['planner.data_plan', 'planner.blocked'],
  review: ['review.start', 'review.accepted', 'review.rejected'],
  train: ['training.status', 'training.done', 'training.failed'],
};

const DECISIONS = new Set([
  'diagnose.clusters', 'diagnose.no_failures', 'planner.data_plan', 'plan.decision', 'spend.approved', 'flywheel.auto_approve',
  'eval.finalized', 'review.accepted', 'training.done', 'flywheel.shipped', 'ticket.checkpoint_deployed', 'dispatch.target_reached',
]);
const isErrorType = (t: string) => {
  const tail = t.split('.').pop() ?? '';
  return tail === 'error' || tail === 'failed' || tail === 'aborted' || tail === 'blocked' || tail === 'rejected' || tail.endsWith('_failed') || tail.endsWith('_error');
};
const isAgent = (e: Pick<Tdm112Event, 'type'>) => e.type === 'agent.step' || e.type === 'agent.thought';
const str = (v: unknown) => (v === undefined || v === null ? '' : String(v));
const clip = (t: string, max = 220) => (Array.from(t).length <= max ? t : `${Array.from(t).slice(0, max - 1).join('').replace(/\s+\S*$/, '')}…`);

/** The events of this station's latest run (the station's own ids; from a few seconds before it started). */
export function awStationEvents(station: Pick<Tdm112StationView, 'eventIds' | 'startedUs'>, events: readonly Tdm112Event[]): Tdm112Event[] {
  const ids = new Set(station.eventIds);
  const from = station.startedUs ? station.startedUs - 5_000_000 : 0;
  return events.filter((e) => ids.has(e.id) && e.tsUs >= from);
}

/** An LLM call in flight at this station: the newest llm_start with no llm_end / llm_error after it. */
export function awLlmInFlight(evs: readonly Tdm112Event[], nowUs: number): { sinceUs: number; label: string } | null {
  for (let i = evs.length - 1; i >= 0; i--) {
    const e = evs[i];
    if (e.type !== 'agent.step') continue;
    const ph = str(e.payload.phase);
    if (ph === 'llm_end' || ph === 'llm_error') return null;
    if (ph === 'llm_start') {
      // A call never runs longer than the CLI timeout (5 min) plus slack: an older start is a lost end row.
      if (nowUs - e.tsUs > 8 * 60 * 1_000_000) return null;
      return { sinceUs: e.tsUs, label: str(e.payload.text).replace(/^Asking Claude\s*/i, '').replace(/^\(|\)$/g, '') };
    }
  }
  return null;
}

export interface AwLiveness {
  mode: AwMode;
  /** "Waiting for operator approval of $17.50" when mode is waiting. */
  waitingFor?: string;
  lastUs?: number;
  ageS?: number;
  freshness: AwFreshness;
  llm: { sinceUs: number; label: string } | null;
}

/**
 * Is the agent at this station working, thinking (a Claude call in flight), waiting on someone, or quiet
 * for too long (stalled)? Done / failed / idle come straight from the station state.
 */
export function awLiveness(station: Tdm112StationView, events: readonly Tdm112Event[], nowUs: number): AwLiveness {
  const evs = awStationEvents(station, events);
  const last = evs[evs.length - 1];
  const lastUs = Math.max(last?.tsUs ?? 0, station.startedUs ?? 0) || undefined;
  const ageS = lastUs && nowUs ? Math.max(0, (nowUs - lastUs) / 1e6) : undefined;
  const lim = AW_LIMITS[station.key];
  const llm = station.state === 'working' ? awLlmInFlight(evs, nowUs) : null;
  const fresh: AwFreshness = ageS === undefined ? 'none' : ageS >= lim.staleS ? 'stale' : ageS >= lim.slowS ? 'slow' : 'fresh';
  const base = { lastUs, ageS, llm };
  switch (station.state) {
    case 'done':
      return { ...base, mode: 'done', freshness: 'none' };
    case 'failed':
      return { ...base, mode: 'failed', freshness: 'none' };
    case 'idle':
      return { ...base, mode: 'idle', freshness: 'none' };
    case 'waiting':
      return { ...base, mode: 'waiting', waitingFor: station.activity, freshness: 'none' };
  }
  if (llm) return { ...base, mode: 'thinking', freshness: fresh };
  if (fresh === 'stale') {
    const quiet = QUIET_MEANS[station.key];
    return quiet ? { ...base, mode: 'waiting', waitingFor: quiet, freshness: 'none' } : { ...base, mode: 'stalled', freshness: 'stale' };
  }
  return { ...base, mode: 'working', freshness: fresh };
}

/** One event row -> a transcript line (null for rows that say nothing new). */
function lineOf(e: Tdm112Event, words: AwWords): AwLine | null {
  const p = e.payload;
  const id = `e${e.id}`;
  if (e.type === 'agent.thought') {
    const ph = str(p.phase);
    const text = str(p.text);
    if (!text) return null;
    return { id, glyph: 'thought', text: ph === 'llm_wait' ? text : clip(`Thinking: ${text}`), tsUs: e.tsUs };
  }
  if (e.type === 'agent.step') {
    const text = clip(str(p.text));
    if (!text) return null;
    const glyph: AwGlyph = p.is_error === true ? 'error' : p.decision === true ? 'decision' : 'step';
    return { id, glyph, text, tsUs: e.tsUs };
  }
  const text = clip(words(e));
  if (!text) return null;
  return { id, glyph: isErrorType(e.type) ? 'error' : DECISIONS.has(e.type) ? 'decision' : 'event', text, tsUs: e.tsUs };
}

export interface AwWorkbench extends AwLiveness {
  station: Tdm112StationKey;
  agent: AwAgent;
  /** The one line under the droid: what it is doing right now. */
  headline: string;
  lines: AwLine[];
}

const MAX_LINES = 80;

/**
 * The workbench of one station: liveness plus the transcript. While the agent works the newest line is
 * `live` (spinner); while Claude thinks a live "Thinking… 12s" line ticks locally between heartbeat rows;
 * while it waits the last line says on what.
 */
export function awWorkbench(station: Tdm112StationView, events: readonly Tdm112Event[], nowUs: number, words: AwWords = fallbackWords): AwWorkbench {
  const live = awLiveness(station, events, nowUs);
  const evs = awStationEvents(station, events);
  const narrated = evs.some((e) => e.type === 'agent.step');
  const covered = new Set(narrated ? COVERED[station.key] ?? [] : []);
  const lines: AwLine[] = [];
  for (const e of evs) {
    if (covered.has(e.type)) continue;
    const l = lineOf(e, words);
    if (!l) continue;
    const prev = lines[lines.length - 1];
    // Heartbeats of one Claude call collapse into one line that keeps the newest count.
    if (e.type === 'agent.thought' && str(e.payload.phase) === 'llm_wait' && prev?.glyph === 'thought' && /^Still thinking/.test(prev.text)) {
      lines[lines.length - 1] = { ...l, id: prev.id };
      continue;
    }
    if (prev && prev.text === l.text && prev.glyph === l.glyph) continue;
    lines.push(l);
  }
  if (live.mode === 'thinking' && live.llm) {
    const s = Math.max(0, Math.round((nowUs - live.llm.sinceUs) / 1e6));
    const text = `Thinking… ${s}s`;
    const prev = lines[lines.length - 1];
    if (prev?.glyph === 'thought' && /^Still thinking/.test(prev.text)) lines[lines.length - 1] = { ...prev, text, live: true };
    else lines.push({ id: 'live-think', glyph: 'thought', text, tsUs: nowUs, live: true });
  } else if (live.mode === 'working' && lines.length) {
    lines[lines.length - 1] = { ...lines[lines.length - 1], live: true };
  } else if (live.mode === 'working') {
    lines.push({ id: 'live-start', glyph: 'step', text: station.activity || 'Starting', tsUs: nowUs, live: true });
  } else if (live.mode === 'waiting' && live.waitingFor) {
    lines.push({ id: 'live-wait', glyph: 'wait', text: live.waitingFor, tsUs: nowUs });
  } else if (live.mode === 'stalled') {
    lines.push({ id: 'live-stall', glyph: 'error', text: `No activity for ${awAgo(live.ageS ?? 0)}. The agent may be stuck.`, tsUs: nowUs });
  } else if (!lines.length) {
    lines.push({ id: 'live-idle', glyph: live.mode === 'failed' ? 'error' : 'event', text: live.mode === 'done' ? 'Done' : live.mode === 'failed' ? station.activity || 'Failed' : station.waitsFor, tsUs: nowUs });
  }

  const agent = AW_AGENT[station.key];
  const lastReal = [...lines].reverse().find((l) => !l.id.startsWith('live-'));
  const headline =
    live.mode === 'thinking'
      ? 'Thinking'
      : live.mode === 'waiting'
        ? live.waitingFor ?? 'Waiting'
        : live.mode === 'stalled'
          ? 'No recent activity'
          : live.mode === 'done'
            ? 'Done'
            : live.mode === 'failed'
              ? lastReal?.text ?? 'Failed'
              : live.mode === 'idle'
                ? station.waitsFor
                : lastReal?.text ?? station.activity;
  return { ...live, station: station.key, agent, headline, lines: lines.slice(-MAX_LINES) };
}

/** "4s", "2m 05s", "1h 03m". */
export function awAgo(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, '0')}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`;
}

export const AW_MODE_WORDS: Record<AwMode, string> = {
  working: 'Working',
  thinking: 'Thinking',
  waiting: 'Waiting',
  stalled: 'Stalled',
  done: 'Done',
  failed: 'Failed',
  idle: 'Idle',
};

/** The newest narrated line (agent.step / agent.thought, heartbeats skipped) of a station's latest run. */
export function awNarration(station: Pick<Tdm112StationView, 'eventIds' | 'startedUs'>, events: readonly Tdm112Event[]): string | undefined {
  const evs = awStationEvents(station, events);
  for (let i = evs.length - 1; i >= 0; i--) {
    const e = evs[i];
    if (!isAgent(e) || str(e.payload.phase) === 'llm_wait') continue;
    const t = str(e.payload.text);
    if (t) return e.type === 'agent.thought' ? `Thinking: ${t}` : t;
  }
  return undefined;
}

/** Liveness of every station (TDM-112 index order) for FactoryIso: the same rule as the cockpit droid. */
export function awFloorLiveness(
  stations: readonly Tdm112StationView[],
  events: readonly Tdm112Event[],
  nowUs: number,
): (AwLiveness & { narration?: string })[] {
  return stations.map((st) => ({ ...awLiveness(st, events, nowUs), narration: awNarration(st, events) }));
}
