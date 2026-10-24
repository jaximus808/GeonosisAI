'use client';

// TDM-76: the agents panel's view model, built on TDM-35's normalised feed.
//
// useAgentFeed (apps/web/src/lib/agentfeed) does the subscriptions (agent_session + event_log) or the
// replay of a recorded run, and normalises every row into AgentFeed v1. toPanelSource turns that
// into the panel's own small shape (sessions + per-session events keyed by their source type), and
// the builders below group droid events into turns, match tool results to calls, and find the
// moments a SpacetimeDB update woke a droid.

import { useEffect, useMemo, useState } from 'react';
import { useAgentFeed as useTdm35Feed, type AgentFeed as Tdm35Feed, type AgentView as Tdm35Agent } from '@/lib/agentfeed/useAgentFeed';
import { toNode, type PredicateNode } from './predicate';

// ------------------------------------------------------------------------------ source shape

export type FeedSession = {
  id: number;
  ticketId: number;
  role: string;
  name: string;
  factorySessionId?: string;
  factorySessionIds: string[];
  status: string;
  currentStep: string;
  waitingFor?: PredicateNode;
  waitingForRaw?: string;
  /** First event seen for the session (agent_session.created_at is not in the feed). */
  createdUs: number;
  lastUpdateUs: number;
};

export type FeedEvent = {
  key: string;
  tsUs: number;
  actor: string;
  /** Source type, e.g. droid.tool_call, agent.resumed. */
  type: string;
  /** For droid.* rows the TDM-30 v1 event shape ({agent_session_id, turn, text, tool, data, ...}); else the payload. */
  payload: Record<string, unknown>;
};

export type PanelSource = {
  ready: boolean;
  mode: 'live' | 'replay';
  error?: string;
  replaySource?: string[];
  sessions: FeedSession[];
  events: FeedEvent[];
  services: Tdm35Agent[];
};

const num = (v: unknown, d = 0): number => (typeof v === 'number' ? v : typeof v === 'bigint' ? Number(v) : typeof v === 'string' && v !== '' && Number.isFinite(Number(v)) ? Number(v) : d);

/** Events kept per agent. The panel shows whole turn timelines, so ask for plenty. */
const PANEL_LIMIT = 400;

export function toPanelSource(feed: Tdm35Feed): Omit<PanelSource, 'ready' | 'mode' | 'error'> {
  const sessions: FeedSession[] = [];
  const events: FeedEvent[] = [];
  const services: Tdm35Agent[] = [];
  for (const a of feed.agents) {
    if (a.kind === 'service') {
      services.push(a);
      continue;
    }
    const sid = a.agent_session_id ?? -1;
    const first = a.events[0]?.ts_us;
    let wf = a.waiting_for && typeof a.waiting_for === 'object' ? (a.waiting_for as object) : undefined;
    let status = a.status;
    if (feed.replay) {
      // A recording has no agent_session row: take status and the predicate from the droid's own events.
      const turnEvents = a.events.filter((e) => e.type === 'droid.turn_started' || e.type === 'droid.turn_completed');
      const lastTurn = turnEvents[turnEvents.length - 1];
      if (lastTurn?.type === 'droid.turn_started') status = 'running';
      else if (lastTurn) status = String(lastTurn.data.status ?? status);
      const lastWait = [...a.events].reverse().find((e) => e.type === 'droid.waiting_for');
      if (!wf && status === 'waiting_for' && lastWait && typeof lastWait.data.predicate === 'object') wf = lastWait.data.predicate as object;
    }
    sessions.push({
      id: sid,
      ticketId: a.ticket_id ?? 0,
      role: a.role,
      // TDM-35 labels are '<name> (<role words> #<id>)'; the card shows role and id itself.
      name: a.label.replace(/\s*\([^()]*#\d+\)$/, ''),
      factorySessionId: a.factory_session_id ?? undefined,
      factorySessionIds: a.factory_session_ids,
      status,
      currentStep: a.current_step,
      waitingFor: toNode(wf),
      waitingForRaw: wf && typeof (wf as { raw?: unknown }).raw === 'string' ? (wf as { raw: string }).raw : undefined,
      createdUs: first ?? a.last_update_us,
      lastUpdateUs: a.last_update_us,
    });
    for (const e of a.events) {
      const payload: Record<string, unknown> = e.type.startsWith('droid.')
        ? { agent_session_id: sid, turn: e.turn, text: e.text, tool: e.tool, factory_session_id: e.data.factory_session_id ?? null, role: e.data.role, data: e.data }
        : e.data;
      events.push({ key: e.id, tsUs: e.ts_us, actor: e.actor, type: e.type, payload });
    }
  }
  return { sessions, events, services, replaySource: feed.replay_source };
}

/** The panel's data, live or (replay) from the recorded run TDM-35 serves. */
export function usePanelSource(opts: { ticketId?: number; replay?: boolean }): PanelSource {
  const { feed, ready, mode, error } = useTdm35Feed({
    ticketId: opts.ticketId,
    limit: PANEL_LIMIT,
    replay: opts.replay ? true : undefined,
    replaySpeed: opts.replay ? 4 : undefined,
  });
  return useMemo(() => ({ ready, mode, error, ...toPanelSource(feed) }), [feed, ready, mode, error]);
}

// ------------------------------------------------------------------------------ view model

export type StepItem = {
  key: string;
  kind: string;
  tsUs: number;
  text: string;
  tool?: string;
  data: Record<string, unknown>;
  /** tool_result matched to its tool_call by tool_use_id. */
  result?: { ok: boolean; text: string; preview?: string };
};

export type ResumeMark = {
  key: string;
  tsUs: number;
  /** A SpacetimeDB update satisfied the predicate and the resume bridge woke the droid. */
  bySpacetime: boolean;
  mode: 'native' | 'restart';
  reason: string;
  predicate?: PredicateNode;
  previousFactorySessionId?: string;
  factorySessionId?: string;
  waitedS?: number;
};

export type Turn = {
  n: number;
  protocol: string; // start | send | native_resume | restart
  factorySessionId?: string;
  startedUs: number;
  prompt?: string;
  /** One-line summary of the brief or message (turn_started text without its 'turn N (protocol): ' prefix). */
  summary?: string;
  steps: StepItem[];
  resume?: ResumeMark;
  restartedFrom?: string;
  completed?: { status: string; credits?: number; seconds?: number; finalText?: string; error?: string };
};

export type FactorySessionUse = { id: string; turns: number[]; protocols: string[]; restart: boolean };

export type AgentView = {
  session: FeedSession;
  turns: Turn[];
  factorySessions: FactorySessionUse[];
  credits?: number;
  resumes: ResumeMark[];
  lastResume?: ResumeMark;
  nativeResumes: number;
  restarts: number;
  stepCount: number;
  isDroid: boolean;
  isTest: boolean;
};

const DROID_ROLES = new Set(['onboarding_droid', 'data_droid', 'training_droid']);

function asResume(e: FeedEvent): ResumeMark {
  const p = e.payload;
  const reason = String(p.reason ?? '');
  const waited = /waited (\d+(?:\.\d+)?)s/.exec(reason);
  return {
    key: e.key,
    tsUs: e.tsUs,
    // The resume bridge (TDM-27) claims with reason "predicate true: <predicate>; <what changed>; ...".
    bySpacetime: reason.startsWith('predicate true:'),
    mode: p.mode === 'restart' ? 'restart' : 'native',
    reason,
    predicate: toNode(p.satisfied_predicate),
    previousFactorySessionId: typeof p.previous_factory_session_id === 'string' ? p.previous_factory_session_id : undefined,
    factorySessionId: typeof p.factory_session_id === 'string' ? p.factory_session_id : undefined,
    waitedS: waited ? Number(waited[1]) : undefined,
  };
}

export function buildAgentView(session: FeedSession, events: FeedEvent[]): AgentView {
  const droidEvents: FeedEvent[] = [];
  const resumes: ResumeMark[] = [];
  for (const e of events) {
    const p = e.payload;
    if (e.type.startsWith('droid.') && num(p.agent_session_id, -1) === session.id) droidEvents.push(e);
    else if (e.type === 'agent.resumed' && num(p.session_id, -1) === session.id) resumes.push(asResume(e));
  }
  // Already in (ts_us, id) order from TDM-35.

  const turns: Turn[] = [];
  const byTurn = new Map<number, Turn>();
  const calls = new Map<string, StepItem>();
  let credits: number | undefined;
  let creditSum = 0;
  let stepCount = 0;
  for (const e of droidEvents) {
    const p = e.payload;
    const kind = e.type.slice(6);
    const n = num(p.turn, 1);
    const data = (p.data ?? {}) as Record<string, unknown>;
    let t = byTurn.get(n);
    if (!t) {
      t = { n, protocol: 'start', startedUs: e.tsUs, steps: [] };
      byTurn.set(n, t);
      turns.push(t);
    }
    if (typeof p.factory_session_id === 'string' && p.factory_session_id) t.factorySessionId = p.factory_session_id;
    const text = typeof p.text === 'string' ? p.text : '';
    if (kind === 'turn_started') {
      t.protocol = String(data.protocol ?? 'start');
      t.startedUs = e.tsUs;
      t.prompt = typeof data.prompt_preview === 'string' ? data.prompt_preview : text;
      t.summary = text.replace(/^turn \d+ \([a-z_]+\): /, '');
      continue;
    }
    if (kind === 'session_restarted') t.restartedFrom = String(data.previous_factory_session_id ?? '');
    if (kind === 'turn_completed') {
      const c = num(data.credits, NaN);
      if (Number.isFinite(c)) creditSum += c;
      const ct = num(data.credits_total, NaN);
      if (Number.isFinite(ct)) credits = Math.max(credits ?? 0, ct);
      t.completed = {
        status: String(data.status ?? (data.success ? 'done' : 'failed')),
        credits: Number.isFinite(c) ? c : undefined,
        seconds: num(data.seconds, NaN),
        finalText: typeof data.final_text === 'string' ? data.final_text : undefined,
        error: typeof data.error === 'string' ? data.error : undefined,
      };
      continue;
    }
    if (kind === 'tool_result') {
      const id = String(data.tool_use_id ?? '');
      const call = calls.get(id);
      const res = { ok: !data.is_error, text, preview: typeof data.preview === 'string' ? data.preview : undefined };
      if (call) call.result = res;
      else t.steps.push({ key: e.key, kind, tsUs: e.tsUs, text, data, result: res });
      continue;
    }
    const item: StepItem = { key: e.key, kind, tsUs: e.tsUs, text, tool: typeof p.tool === 'string' ? p.tool : undefined, data };
    if (kind === 'tool_call' && data.tool_use_id) calls.set(String(data.tool_use_id), item);
    t.steps.push(item);
    stepCount++;
  }
  turns.sort((a, b) => a.n - b.n);

  // Pin each resume to the first turn that started after it (it is what that turn answers).
  resumes.sort((a, b) => a.tsUs - b.tsUs);
  for (const r of resumes) {
    const t = turns.find((x) => x.startedUs >= r.tsUs - 2_000_000 && !x.resume && x.protocol !== 'start');
    if (t) t.resume = r;
  }

  // Which Factory session ids carried which turns (Protocol A keeps one id; B starts a new one).
  const ids = [...session.factorySessionIds];
  for (const t of turns) if (t.factorySessionId && !ids.includes(t.factorySessionId)) ids.push(t.factorySessionId);
  const factorySessions: FactorySessionUse[] = ids.map((id, i) => {
    const ts = turns.filter((t) => t.factorySessionId === id);
    return { id, turns: ts.map((t) => t.n), protocols: [...new Set(ts.map((t) => t.protocol))], restart: i > 0 };
  });

  const nativeResumes = turns.filter((t) => t.protocol === 'native_resume').length;
  const restarts = Math.max(turns.filter((t) => t.protocol === 'restart').length, factorySessions.length - 1);
  return {
    session,
    turns,
    factorySessions,
    credits: credits ?? (creditSum > 0 ? creditSum : undefined),
    resumes,
    lastResume: resumes[resumes.length - 1],
    nativeResumes,
    restarts,
    stepCount,
    isDroid: DROID_ROLES.has(session.role),
    isTest: session.name.startsWith('test.') || session.name.startsWith('test-'),
  };
}

// ------------------------------------------------------------------------------ orchestrator agents

/** What each of the orchestrator's own agents (TDM-35 service keys) does, in a few words. */
export const SERVICE_DOES: Record<string, string> = {
  clustering: 'groups failed eval trials into failure modes',
  planner: 'decides what data to buy next and why',
  reviewer: 'accepts or rejects each recorded episode',
  qa: 'checks bought and recorded data before training',
  dispatcher: 'turns the data plan into paid tasks',
  eval: 'runs the eval trials on the robot',
  flywheel: 'moves the ticket from stage to stage',
};
const SERVICE_ORDER = ['clustering', 'planner', 'reviewer', 'qa', 'dispatcher', 'eval', 'flywheel'];
const SERVICE_LABEL: Record<string, string> = {
  clustering: 'Failure clustering', planner: 'Planner', reviewer: 'Reviewer', qa: 'Dataset QA',
  dispatcher: 'Dispatcher', eval: 'Eval runner', flywheel: 'Flywheel',
};

export type OrchAgentView = {
  key: string;
  label: string;
  does: string;
  status: string; // active | idle | error | none
  count: number;
  last?: { tsUs: number; text: string; type: string };
  recent: { key: string; tsUs: number; type: string; text: string; isError: boolean }[];
};

/** Every orchestrator agent, including the ones that have not acted yet (so judges see the cast). */
export function buildOrchAgents(services: Tdm35Agent[]): OrchAgentView[] {
  const by = new Map(services.map((s) => [s.role, s]));
  const keys = [...SERVICE_ORDER, ...services.map((s) => s.role).filter((k) => !SERVICE_ORDER.includes(k))];
  return keys.map((k) => {
    const s = by.get(k);
    const evs = s?.events ?? [];
    const last = evs[evs.length - 1];
    return {
      key: k,
      label: SERVICE_LABEL[k] ?? s?.label ?? k,
      does: SERVICE_DOES[k] ?? '',
      status: s && s.event_count > 0 ? s.status : 'none',
      count: s?.event_count ?? 0,
      last: last ? { tsUs: last.ts_us, text: last.text, type: last.type } : undefined,
      recent: evs.slice(-50).map((e) => ({ key: e.id, tsUs: e.ts_us, type: e.type, text: e.text, isError: e.is_error })),
    };
  });
}

// ------------------------------------------------------------------------------ the hook

export type PanelModel = {
  ready: boolean;
  mode: 'live' | 'replay';
  error?: string;
  replaySource?: string[];
  agents: AgentView[];
  orchestrator: OrchAgentView[];
};

/** Everything the agents panel renders. */
export function usePanelModel(opts: { ticketId?: number; replay?: boolean } = {}): PanelModel {
  const source = usePanelSource(opts);
  return useMemo(() => {
    const agents = source.sessions
      .map((s) => buildAgentView(s, source.events))
      .sort((a, b) => b.session.lastUpdateUs - a.session.lastUpdateUs);
    return {
      ready: source.ready, mode: source.mode, error: source.error, replaySource: source.replaySource,
      agents, orchestrator: buildOrchAgents(source.services),
    };
  }, [source]);
}

/** A clock that re-renders every `ms` (elapsed timers, "updated 3 s ago"). */
export function useNow(ms = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}
