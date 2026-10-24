'use client';

// TDM-76: the agents panel's data, behind one small adapter.
//
// A FeedSource is "agent_session rows + agent-related event rows", already normalised. Today it
// comes from two direct subscriptions (agent_session, event_log) or from a recorded droid jsonl
// (replay). When TDM-35's normalised feed lands, swap useLiveFeedSource for its hook: nothing
// below the FeedSource line needs to change.

import { useEffect, useMemo, useState } from 'react';
import { useRows } from '@/lib/spacetime/hooks';
import { parsePayload } from '@/lib/eventlog/classify';
import { parsePredicate, toNode, type PredicateNode } from './predicate';

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
  resumePayload?: unknown;
  createdUs: number;
  lastUpdateUs: number;
  /** Rebuilt from events because there is no agent_session row (replay of a recording). */
  derived?: boolean;
};

export type FeedEvent = {
  key: string;
  /** event_log id, when the row came from event_log. */
  id?: number;
  tsUs: number;
  actor: string;
  /** event_log type, e.g. droid.tool_call, agent.resumed, planner.data_plan. */
  type: string;
  payload: Record<string, unknown>;
};

export type FeedSource = {
  ready: boolean;
  mode: 'live' | 'replay';
  sessions: FeedSession[];
  events: FeedEvent[];
  /** Replay only: which recording and how far through it we are. */
  replay?: { file: string; shown: number; total: number; speed: number };
};

const num = (v: unknown, d = 0): number => (typeof v === 'number' ? v : typeof v === 'bigint' ? Number(v) : typeof v === 'string' && v !== '' && Number.isFinite(Number(v)) ? Number(v) : d);

function jsonArr(s: string): string[] {
  try {
    const v = JSON.parse(s);
    return Array.isArray(v) ? v.map(String) : [];
  } catch {
    return [];
  }
}

function jsonAny(s: string | undefined): unknown {
  if (!s) return undefined;
  try {
    return JSON.parse(s);
  } catch {
    return s;
  }
}

/** Rows this panel cares about: droid steps, session lifecycle, and the orchestrator's own agents. */
export function isAgentEventType(type: string): boolean {
  return ORCH_AGENTS.some((a) => a.match(type)) || type.startsWith('droid.') || type.startsWith('agent.');
}

/** Live source: direct subscriptions to agent_session and event_log (until TDM-35 lands). */
export function useLiveFeedSource(): FeedSource {
  const [sessionRows, sReady] = useRows('agentSession');
  const [eventRows, eReady] = useRows('eventLog');

  const sessions = useMemo<FeedSession[]>(
    () =>
      sessionRows.map((r) => {
        const raw = r.waitingForJson ?? undefined;
        return {
          id: Number(r.id),
          ticketId: Number(r.ticketId),
          role: r.role,
          name: r.name,
          factorySessionId: r.factorySessionId ?? undefined,
          factorySessionIds: jsonArr(r.factorySessionIdsJson),
          status: r.status,
          currentStep: r.currentStep,
          waitingFor: parsePredicate(raw),
          waitingForRaw: raw,
          resumePayload: jsonAny(r.resumePayloadJson ?? undefined),
          createdUs: Number(r.createdAt.microsSinceUnixEpoch),
          lastUpdateUs: Number(r.lastUpdate.microsSinceUnixEpoch),
        };
      }),
    [sessionRows],
  );

  const events = useMemo<FeedEvent[]>(() => {
    const out: FeedEvent[] = [];
    for (const r of eventRows) {
      if (!isAgentEventType(r.type)) continue;
      out.push({
        key: `e${r.id}`,
        id: Number(r.id),
        tsUs: Number(r.ts.microsSinceUnixEpoch),
        actor: r.actor,
        type: r.type,
        payload: parsePayload(r.payloadJson) ?? { raw: r.payloadJson },
      });
    }
    return out.sort((a, b) => a.id! - b.id!);
  }, [eventRows]);

  return { ready: sReady && eReady, mode: 'live', sessions, events };
}

// ------------------------------------------------------------------------------ replay source

type RecordedEvent = { kind: string; agent_session_id: number; role: string; ts_us: number; turn: number; seq: number } & Record<string, unknown>;

/**
 * Replays a recorded droid session (services/factory/work/state/<id>.events.jsonl, served by
 * /api/tdm76/recording) at `speed`x its real pacing, so the panel can be shown without spending
 * credits. Labelled REPLAY in the UI: these are real past events, not live ones.
 */
export function useReplayFeedSource(file: string | undefined, speed = 4): FeedSource {
  const [all, setAll] = useState<RecordedEvent[] | undefined>(undefined);
  const [shown, setShown] = useState(0);

  useEffect(() => {
    if (!file) return;
    let alive = true;
    setAll(undefined);
    setShown(0);
    fetch(`/api/tdm76/recording?file=${encodeURIComponent(file)}`)
      .then((r) => (r.ok ? r.text() : ''))
      .then((t) => {
        if (!alive) return;
        const evs = t
          .split('\n')
          .filter(Boolean)
          .map((l) => {
            try {
              return JSON.parse(l) as RecordedEvent;
            } catch {
              return undefined;
            }
          })
          .filter((e): e is RecordedEvent => !!e && typeof e.kind === 'string');
        setAll(evs);
      })
      .catch(() => alive && setAll([]));
    return () => {
      alive = false;
    };
  }, [file]);

  useEffect(() => {
    if (!all || shown >= all.length) return;
    const gapMs = shown === 0 ? 400 : Math.min(2500, Math.max(120, (all[shown].ts_us - all[shown - 1].ts_us) / 1000 / speed));
    const t = setTimeout(() => setShown((s) => s + 1), gapMs);
    return () => clearTimeout(t);
  }, [all, shown, speed]);

  return useMemo<FeedSource>(() => {
    if (!file) return { ready: true, mode: 'replay', sessions: [], events: [] };
    const evs = (all ?? []).slice(0, shown);
    // Re-time the replay to "now" so elapsed/age read naturally while it plays.
    const offset = all && all.length ? Date.now() * 1000 - (evs.length ? evs[evs.length - 1].ts_us : all[0].ts_us) : 0;
    const events: FeedEvent[] = evs.map((e, i) => ({
      key: `r${i}`,
      tsUs: e.ts_us + offset,
      actor: `droid:${String(e.role).replace(/_droid$/, '')}`,
      type: `droid.${e.kind}`,
      payload: { ...e, ts_us: e.ts_us + offset },
    }));
    return {
      ready: all !== undefined,
      mode: 'replay',
      sessions: deriveSessions(events),
      events,
      replay: { file, shown, total: all?.length ?? 0, speed },
    };
  }, [file, all, shown, speed]);
}

/** agent_session-like rows rebuilt from droid events (only used when there is no real row). */
export function deriveSessions(events: FeedEvent[]): FeedSession[] {
  const by = new Map<number, FeedSession>();
  for (const e of events) {
    if (!e.type.startsWith('droid.')) continue;
    const p = e.payload;
    const id = num(p.agent_session_id, -1);
    if (id < 0) continue;
    const kind = e.type.slice(6);
    let s = by.get(id);
    if (!s) {
      s = {
        id, ticketId: 0, role: String(p.role ?? 'droid'), name: String(p.role ?? 'droid'), factorySessionIds: [],
        status: 'starting', currentStep: '', createdUs: e.tsUs, lastUpdateUs: e.tsUs, derived: true,
      };
      by.set(id, s);
    }
    const fsid = typeof p.factory_session_id === 'string' ? p.factory_session_id : undefined;
    if (fsid && !s.factorySessionIds.includes(fsid)) s.factorySessionIds.push(fsid);
    if (fsid) s.factorySessionId = fsid;
    s.lastUpdateUs = e.tsUs;
    const data = (p.data ?? {}) as Record<string, unknown>;
    if (kind === 'turn_started') {
      s.status = 'running';
      s.waitingFor = undefined;
    }
    if (kind === 'waiting_for') {
      s.status = 'waiting_for';
      s.waitingFor = toNode(data.predicate);
      s.resumePayload = data.resume_payload;
      const pred = data.predicate as Record<string, unknown> | undefined;
      if (pred && pred.ticket_id !== undefined) s.ticketId = num(pred.ticket_id);
    }
    if (kind === 'turn_completed' && typeof data.status === 'string') s.status = data.status;
    if (kind !== 'tool_result' && kind !== 'turn_completed' && typeof p.text === 'string') s.currentStep = kind === 'assistant_text' ? `says: ${p.text}` : p.text;
  }
  return [...by.values()];
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
  droidEvents.sort((a, b) => (a.id !== undefined && b.id !== undefined ? a.id - b.id : a.tsUs - b.tsUs));

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

/**
 * The orchestrator's own agents run in-process (no agent_session row, no Factory session), so the
 * panel shows them from the rows they write. Order matters: the first match wins.
 */
export const ORCH_AGENTS: { key: string; label: string; does: string; match: (type: string) => boolean }[] = [
  { key: 'clustering', label: 'Failure clustering', does: 'groups failed eval trials into failure modes', match: (t) => t === 'planner.failure_cluster' || t.startsWith('diagnose.') },
  { key: 'planner', label: 'Planner', does: 'decides what data to buy next and why', match: (t) => t.startsWith('planner.') },
  { key: 'reviewer', label: 'Reviewer', does: 'accepts or rejects each recorded episode', match: (t) => t === 'review.accepted' || t === 'review.rejected' || t.startsWith('review.') },
  { key: 'qa', label: 'Dataset QA', does: 'checks bought and recorded data before training', match: (t) => t.startsWith('qa.') || t.startsWith('dataset_qa.') || t.startsWith('llm_qa.') },
  { key: 'dispatcher', label: 'Dispatcher', does: 'turns the data plan into paid tasks', match: (t) => t.startsWith('dispatch.') },
  { key: 'bridge', label: 'Resume bridge', does: 'watches SpacetimeDB and wakes parked droids', match: (t) => t.startsWith('resume_bridge.') || t.startsWith('agent.resume_') },
];

export type OrchAgentView = {
  key: string;
  label: string;
  does: string;
  count: number;
  last?: FeedEvent;
  recent: FeedEvent[];
};

export function buildOrchAgents(events: FeedEvent[], ticketId?: number): OrchAgentView[] {
  const views = ORCH_AGENTS.map((a) => ({ key: a.key, label: a.label, does: a.does, count: 0, recent: [] as FeedEvent[], last: undefined as FeedEvent | undefined }));
  for (const e of events) {
    if (e.type.startsWith('droid.')) continue;
    if (ticketId !== undefined && num(e.payload.ticket_id ?? e.payload.ticketId, -1) !== ticketId) continue;
    const i = ORCH_AGENTS.findIndex((a) => a.match(e.type));
    if (i < 0) continue;
    const v = views[i];
    v.count++;
    v.last = e;
    v.recent.push(e);
    if (v.recent.length > 50) v.recent.shift();
  }
  return views;
}

// ------------------------------------------------------------------------------ the hook

export type AgentFeed = {
  ready: boolean;
  mode: 'live' | 'replay';
  replay?: FeedSource['replay'];
  agents: AgentView[];
  orchestrator: OrchAgentView[];
};

/** Everything the agents panel renders, from whichever source is active. */
export function useAgentFeed(source: FeedSource, opts: { ticketId?: number } = {}): AgentFeed {
  const { ticketId } = opts;
  return useMemo(() => {
    const sessions = source.sessions.filter((s) => ticketId === undefined || s.ticketId === ticketId);
    const agents = sessions
      .map((s) => buildAgentView(s, source.events))
      .sort((a, b) => b.session.lastUpdateUs - a.session.lastUpdateUs);
    return { ready: source.ready, mode: source.mode, replay: source.replay, agents, orchestrator: buildOrchAgents(source.events, ticketId) };
  }, [source, ticketId]);
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
