'use client';

// TDM-35: the agents panel's data hook (consumer: TDM-76).
//
//   const { feed, ready, mode, error } = useAgentFeed();                    // live, all agents
//   const { feed } = useAgentFeed({ ticketId: 3, limit: 50 });              // one ticket
//   const { feed } = useAgentFeed({ replay: true, replaySpeed: 4 });        // recorded run, played back 4x
//   const { feed } = useAgentFeed({ replay: 'auto' });                      // live; replay while SpacetimeDB is down
//
// Live mode uses only SpacetimeDB subscriptions (agentSession + eventLog) and buildAgentFeed.
// Replay mode fetches the recorded droid run from the orchestrator (GET /agents/replay/source) and
// runs the same normaliser; every agent and event then has `replay: true` and `feed.replay` is true,
// so the panel must show a "replay" label. Shape: ./normalize.ts and services/factory/README.md.

import { useEffect, useMemo, useState } from 'react';
import { useConnectionStatus, useRows } from '@/lib/spacetime/hooks';
import {
  buildAgentFeed, DEFAULT_LIMIT, eventInputFromRow, sessionInputFromRow,
  type AgentFeed, type EventInput, type SessionInput,
} from './normalize';

export * from './normalize';

const ORCH = (process.env.NEXT_PUBLIC_ORCHESTRATOR_URL || 'http://localhost:8000').replace(/\/$/, '');

export type AgentFeedOptions = {
  ticketId?: number | bigint | null;
  /** Events kept per agent (newest). Default 20. */
  limit?: number;
  /** true: recorded run. 'auto': live, but the recording while SpacetimeDB is disconnected. */
  replay?: boolean | 'auto';
  /** Replay only: >0 plays the recording back at this speed (1 = real time), looping; 0 shows it all. */
  replaySpeed?: number;
};

export type AgentFeedState = {
  feed: AgentFeed;
  ready: boolean;
  mode: 'live' | 'replay';
  error?: string;
};

type ReplaySource = { sessions: SessionInput[]; events: EventInput[]; replay_source?: string[] };

export function useAgentFeed(opts: AgentFeedOptions = {}): AgentFeedState {
  const limit = opts.limit ?? DEFAULT_LIMIT;
  const ticketId = opts.ticketId === undefined || opts.ticketId === null ? null : Number(opts.ticketId);
  const { status } = useConnectionStatus();
  const replay = opts.replay === true || (opts.replay === 'auto' && status === 'disconnected');

  const [sessionRows, sessionsReady] = useRows('agentSession');
  const [eventRows, eventsReady] = useRows('eventLog');

  const sessions = useMemo<SessionInput[]>(() => sessionRows.map(sessionInputFromRow), [sessionRows]);
  const events = useMemo<EventInput[]>(() => eventRows.map(eventInputFromRow), [eventRows]);

  const [source, setSource] = useState<ReplaySource | null>(null);
  const [error, setError] = useState<string | undefined>();
  useEffect(() => {
    if (!replay || source) return;
    let cancelled = false;
    fetch(`${ORCH}/agents/replay/source`)
      .then((r) => (r.ok ? r.json() : r.text().then((t) => Promise.reject(new Error(`${r.status} ${t.slice(0, 200)}`)))))
      .then((j: ReplaySource) => !cancelled && setSource(j))
      .catch((e: Error) => !cancelled && setError(`replay unavailable: ${e.message}`));
    return () => {
      cancelled = true;
    };
  }, [replay, source]);

  // Playback clock: re-render a few times a second while a replay is playing.
  const speed = replay ? opts.replaySpeed ?? 0 : 0;
  const [startedAt] = useState(() => Date.now());
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!speed) return;
    const t = setInterval(() => setTick((n) => n + 1), 250);
    return () => clearInterval(t);
  }, [speed]);

  const feed = useMemo<AgentFeed>(() => {
    if (!replay) return buildAgentFeed(sessions, events, { ticketId, limit });
    if (!source) return { v: 1, replay: true, generated_at_us: Date.now() * 1000, agents: [] };
    let evs = source.events;
    let ses = source.sessions;
    if (speed > 0 && evs.length) {
      // Rebase the recording onto "now" and reveal it at `speed`, looping with a 5 s pause.
      const t0 = Math.min(...evs.map((e) => e.ts_us));
      const span = Math.max(...evs.map((e) => e.ts_us)) - t0;
      const loop = span / speed + 5_000_000;
      const elapsedUs = ((Date.now() - startedAt) * 1000) % loop;
      const cursor = t0 + elapsedUs * speed;
      const shift = Date.now() * 1000 - cursor;
      evs = evs.filter((e) => e.ts_us <= cursor).map((e) => rebase(e, shift));
      ses = ses.map((s) => ({ ...s, last_update_us: 0 }));
    }
    const f = buildAgentFeed(ses, evs, { ticketId, limit, replay: true });
    return { ...f, replay_source: source.replay_source };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [replay, source, sessions, events, ticketId, limit, speed, tick]);

  return {
    feed,
    ready: replay ? source !== null || error !== undefined : sessionsReady && eventsReady,
    mode: replay ? 'replay' : 'live',
    error: replay ? error : undefined,
  };
}

function rebase(e: EventInput, shift: number): EventInput {
  const p = e.payload;
  return {
    ...e,
    ts_us: e.ts_us + shift,
    payload: p && typeof p.ts_us === 'number' ? { ...p, ts_us: (p.ts_us as number) + shift } : p,
  };
}
