'use client';

// TDM-112: live data for one ticket's factory floor. Every input is a SpacetimeDB subscription; the
// model (./floor.ts) is pure. Event rows are parsed once per change of the event_log table.

import { useEffect, useMemo, useState } from 'react';
import { useRows } from '@/lib/spacetime/hooks';
import { parsePredicate, predicateWords } from '@/lib/tdm76/predicate';
import { tdm112Floor, tdm112IsDroidRole, type Tdm112Event, type Tdm112Floor } from './floor';

type Obj = Record<string, unknown>;

function parse(text: string): Obj | null {
  try {
    const v = JSON.parse(text);
    return v && typeof v === 'object' && !Array.isArray(v) ? (v as Obj) : null;
  } catch {
    return null;
  }
}

type EvRow = { id: bigint; ts: { microsSinceUnixEpoch: bigint }; actor: string; type: string; payloadJson: string };

/** event_log rows -> this ticket's events, llm.call rows and agent.resumed rows (oldest first). */
export function tdm112SplitEvents(rows: readonly EvRow[], ticketId: number) {
  const events: Tdm112Event[] = [];
  const llmCalls: Tdm112Event[] = [];
  const resumes: Tdm112Event[] = [];
  for (const r of rows) {
    const isLlm = r.type === 'llm.call';
    const isResume = r.type === 'agent.resumed';
    if (!isLlm && !isResume && !r.payloadJson.includes('ticket_id')) continue;
    const payload = parse(r.payloadJson);
    if (!payload) continue;
    const e: Tdm112Event = { id: Number(r.id), tsUs: Number(r.ts.microsSinceUnixEpoch), actor: r.actor, type: r.type, payload };
    if (isLlm) llmCalls.push(e);
    else if (isResume) resumes.push(e);
    if (!isLlm && Number(payload.ticket_id) === ticketId) events.push(e);
  }
  const byTs = (a: Tdm112Event, b: Tdm112Event) => a.tsUs - b.tsUs || a.id - b.id;
  return { events: events.sort(byTs), llmCalls: llmCalls.sort(byTs), resumes: resumes.sort(byTs) };
}

/** A clock that ticks every `ms`. 0 until mounted, so server and first client render agree. */
export function useTdm112Now(ms = 1000): number {
  const [now, setNow] = useState(0);
  useEffect(() => {
    setNow(Date.now());
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}

const describe = (json: string) => predicateWords(parsePredicate(json));

export type Tdm112FloorData = { floor: Tdm112Floor | null; ready: boolean; events: Tdm112Event[]; taskDescription?: string };

export function useTdm112Floor(ticketId: bigint): Tdm112FloorData {
  const tid = ticketId;
  const [tickets, ready] = useRows('ticket', (r) => r.id.eq(tid));
  const [evalRuns] = useRows('evalRun', (r) => r.ticketId.eq(tid));
  const [evalTrials] = useRows('evalTrial');
  const [clusters] = useRows('failureCluster', (r) => r.ticketId.eq(tid));
  const [plans] = useRows('dataPlan', (r) => r.ticketId.eq(tid));
  const [tasks] = useRows('task', (r) => r.ticketId.eq(tid));
  const [episodes] = useRows('episode', (r) => r.ticketId.eq(tid));
  const [reviews] = useRows('review', (r) => r.ticketId.eq(tid));
  const [trainingRuns] = useRows('trainingRun', (r) => r.ticketId.eq(tid));
  const [settings] = useRows('runSettings', (r) => r.ticketId.eq(tid));
  const [sessions] = useRows('agentSession', (r) => r.ticketId.eq(tid));
  const [eventRows] = useRows('eventLog');

  const split = useMemo(() => tdm112SplitEvents(eventRows, Number(tid)), [eventRows, tid]);
  const runIds = useMemo(() => new Set(evalRuns.map((r) => r.id)), [evalRuns]);
  const myTrials = useMemo(() => evalTrials.filter((t) => runIds.has(t.evalRunId)), [evalTrials, runIds]);
  const droids = useMemo(() => sessions.filter((x) => tdm112IsDroidRole(x.role)), [sessions]);
  // The model only needs "now" for the 10 s wake-up pulse; 2 s granularity is plenty.
  const now = useTdm112Now(2000);
  const bucket = Math.floor(now / 2000);
  const ticket = tickets[0];

  const floor = useMemo(() => {
    if (!ticket) return null;
    return tdm112Floor({
      ticket,
      maxIterations: settings[0]?.maxIterations,
      evalRuns,
      evalTrials: myTrials,
      clusters,
      plans,
      tasks,
      episodes,
      reviews,
      trainingRuns,
      events: split.events,
      llmCalls: split.llmCalls,
      resumes: split.resumes,
      sessions: droids,
      describePredicate: describe,
      nowUs: bucket * 2000 * 1000,
    });
  }, [ticket, settings, evalRuns, myTrials, clusters, plans, tasks, episodes, reviews, trainingRuns, split, droids, bucket]);

  return { floor, ready, events: split.events, taskDescription: ticket?.taskDescription };
}
