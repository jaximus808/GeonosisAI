'use client';
// TDM-96: useRobotLive(robotId) -> { state, hz, latencyMs, source, stale } (+ transport details).
//
// Live robot state for the browser from the robot's `/ws/state` (rdf-state/1, ~30 Hz; protocol in
// ./liveFeed.ts and services/robot-client/src/rdf_robot_client/stream.py). Contract: docs/WORKER_GUIDE.md,
// "E15 Robot view contract"; types in ./types.ts.
//
// Access: a short-lived VIEW-scope stream token (read-only: state + cameras, never /control) from
// POST /api/robotview/state-token, which is gated by operator login (robot_id) or, with
// `opts.taskId` + `opts.identity`, by being the live claimant of that task. The token is refreshed at
// half-life on the open socket; on close we reconnect with a fresh grant (backoff 1 s .. 10 s).
//
// Fallback: when the stream cannot be reached (no grant, robot offline, socket down) the hook reads the
// SpacetimeDB robot row (status + last_heartbeat, ~5 s) and reports transport 'heartbeat'. A heartbeat
// carries no joints, so `state` stays null and `stale` true then: nothing is ever made up.
//
// `source` is 'sim' only when the robot says so (the state message's `sim`, or the robot row's
// camera_config_json.sim); anything else is 'live'. `opts.fixture` plays the recorded sim fixture
// (fixtures/sim_arm_motion.json) instead, with source 'sim' and transport 'fixture'.
//
// `state` re-renders at the feed rate. For a render loop (three.js) read `stateRef.current` instead.

import { useEffect, useMemo, useRef, useState, type MutableRefObject } from 'react';
import { BROWSER_TOKEN_KEY } from '@/lib/spacetime/config';
import { useRows } from '@/lib/spacetime/hooks';
import {
  ClockSync,
  RateMeter,
  RollingMedian,
  STALE_MS,
  fixtureFrameAt,
  parseStateMessage,
  stateUrlFrom,
  toLiveState,
  type RobotStateFixture,
} from './liveFeed';
import type { RobotLive, RobotLiveSource, RobotLiveState } from './types';

export type RobotLiveTransport = 'ws' | 'heartbeat' | 'fixture' | 'none';

export interface RobotStateGrant {
  token: string;
  exp: number; // unix s
  state_url?: string | null;
  stream_url?: string | null;
  scope?: string;
  sim?: boolean;
}

export interface UseRobotLiveOptions {
  /** false = do nothing (no socket, no grant). Default true. */
  enabled?: boolean;
  /** Play the recorded sim fixture instead of a robot (labelled sim). */
  fixture?: boolean;
  /** Claimant path: the task this browser has claimed, and its SpacetimeDB identity (hex). */
  taskId?: number | bigint | null;
  identity?: string | null;
  /** Dev / ops: connect straight to this /ws/state URL with this token (e.g. `stream_token mint --scope view`). */
  url?: string;
  token?: string;
  /** Override the grant fetch entirely. */
  getGrant?: () => Promise<RobotStateGrant>;
  staleMs?: number;
}

export interface RobotLiveResult extends RobotLive {
  transport: RobotLiveTransport;
  /** Latest state, updated without re-rendering (for requestAnimationFrame consumers). */
  stateRef: MutableRefObject<RobotLiveState | null>;
  /** Why the stream is not live (grant refused, socket closed, ...), for a status line. */
  error: string | null;
  /** From the SpacetimeDB robot row: coarse status and heartbeat age. */
  heartbeat: { status: string; lastHeartbeatMs: number | null } | null;
  rttMs: number | null;
}

class GrantError extends Error {
  constructor(
    public status: number,
    detail: string,
  ) {
    super(detail);
  }
}

function viewGrant(robotId: number, taskId?: number | bigint | null, identity?: string | null): () => Promise<RobotStateGrant> {
  return async () => {
    let body: Record<string, unknown> = { robot_id: robotId };
    if (taskId != null && identity) {
      let token: string | null = null;
      try {
        token = window.localStorage.getItem(BROWSER_TOKEN_KEY);
      } catch {
        /* storage blocked */
      }
      if (token) body = { task_id: Number(taskId), identity, spacetime_token: token };
    }
    const res = await fetch('/api/robotview/state-token', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
      cache: 'no-store',
    });
    const b = (await res.json().catch(() => ({}))) as RobotStateGrant & { detail?: string };
    if (!res.ok) throw new GrantError(res.status, typeof b.detail === 'string' ? b.detail : res.statusText);
    return b;
  };
}

function staticGrant(url: string, token: string): () => Promise<RobotStateGrant> {
  return async () => {
    let exp = Date.now() / 1000 + 60;
    try {
      exp = JSON.parse(atob(token.split('.')[0].replace(/-/g, '+').replace(/_/g, '/'))).exp ?? exp;
    } catch {
      /* opaque */
    }
    return { token, exp, state_url: url };
  };
}

function rowIsSim(cameraConfigJson: string | undefined): boolean {
  if (!cameraConfigJson) return false;
  try {
    return JSON.parse(cameraConfigJson)?.sim === true;
  } catch {
    return false;
  }
}

export function useRobotLive(robotId: number | bigint | null | undefined, opts: UseRobotLiveOptions = {}): RobotLiveResult {
  const id = robotId == null ? null : Number(robotId);
  const enabled = opts.enabled !== false && (id != null || opts.fixture || !!opts.url);
  const staleMs = opts.staleMs ?? STALE_MS;

  // SpacetimeDB robot row: the heartbeat fallback and the sim label.
  const [rows] = useRows('robot', (r) => r.id.eq(BigInt(id ?? 0)));
  const row = id != null ? rows[0] : undefined;
  const heartbeat = useMemo(
    () =>
      row
        ? {
            status: row.status,
            lastHeartbeatMs: row.lastHeartbeat ? Number(row.lastHeartbeat.microsSinceUnixEpoch / 1000n) : null,
          }
        : null,
    [row],
  );
  const rowSim = rowIsSim(row?.cameraConfigJson);

  const [state, setState] = useState<RobotLiveState | null>(null);
  const [hz, setHz] = useState(0);
  const [latencyMs, setLatency] = useState<number | null>(null);
  const [rttMs, setRtt] = useState<number | null>(null);
  const [stale, setStale] = useState(true);
  const [msgSim, setMsgSim] = useState<boolean | null>(null);
  const [transport, setTransport] = useState<RobotLiveTransport>('none');
  const [error, setError] = useState<string | null>(null);
  const stateRef = useRef<RobotLiveState | null>(null);
  const lastRx = useRef<number>(0);

  const grantKey = `${id}|${opts.taskId ?? ''}|${opts.identity ?? ''}|${opts.url ?? ''}|${opts.token ?? ''}`;
  const getGrantRef = useRef(opts.getGrant);
  getGrantRef.current = opts.getGrant;

  useEffect(() => {
    // reset on robot / mode change: never show one robot's state under another's name
    stateRef.current = null;
    lastRx.current = 0;
    setState(null);
    setHz(0);
    setLatency(null);
    setMsgSim(null);
    setStale(true);
    setError(null);
    setTransport('none');
    if (!enabled) return;

    let dead = false;
    const rate = new RateMeter(1000);
    const clock = new ClockSync();
    const lat = new RollingMedian(30);
    const timers: ReturnType<typeof setTimeout>[] = [];
    const later = (fn: () => void, ms: number) => timers.push(setTimeout(fn, ms));

    const deliver = (s: RobotLiveState, sim: boolean, recvMs: number, latency: number | null) => {
      stateRef.current = s;
      lastRx.current = recvMs;
      rate.tick(recvMs);
      if (latency != null) lat.push(latency);
      setState(s);
      setMsgSim(sim);
    };

    // 10 Hz: hz / latency / stale readouts (not per message; unchanged values do not re-render)
    const meter = setInterval(() => {
      const now = Date.now();
      setHz(Math.round(rate.hz(now) * 10) / 10);
      setLatency(lat.value);
      setRtt(clock.rttMs);
      setStale(!lastRx.current || now - lastRx.current > staleMs);
    }, 100);

    // ---------------------------------------------------------------- fixture
    if (opts.fixture) {
      let raf = 0;
      import('./fixtures/sim_arm_motion.json').then((mod) => {
        if (dead) return;
        const fx = (mod.default ?? mod) as unknown as RobotStateFixture;
        const start = Date.now();
        setTransport('fixture');
        let lastT = 0;
        const step = () => {
          if (dead) return;
          const now = Date.now();
          if (now - lastT >= 1000 / fx.hz - 1) {
            lastT = now;
            const f = fixtureFrameAt(fx, start, now);
            deliver(toLiveState(f), true, now, 0);
          }
          raf = requestAnimationFrame(step);
        };
        raf = requestAnimationFrame(step);
      });
      return () => {
        dead = true;
        cancelAnimationFrame(raf);
        clearInterval(meter);
      };
    }

    // ---------------------------------------------------------------- live socket
    const getGrant =
      getGrantRef.current ?? (opts.url && opts.token ? staticGrant(opts.url, opts.token) : viewGrant(id as number, opts.taskId, opts.identity));
    let ws: WebSocket | null = null;
    let backoff = 1000;
    let pinger: ReturnType<typeof setInterval> | null = null;

    const retry = (why: string) => {
      if (dead) return;
      setError(why);
      setTransport('heartbeat');
      later(connect, backoff);
      backoff = Math.min(backoff * 2, 10_000);
    };

    const scheduleRefresh = (exp: number) => {
      const ms = Math.max(5_000, ((exp - Date.now() / 1000) * 1000) / 2);
      later(async () => {
        if (dead || !ws || ws.readyState !== WebSocket.OPEN) return;
        try {
          const g = await getGrant();
          if (dead || !ws || ws.readyState !== WebSocket.OPEN) return;
          ws.send(JSON.stringify({ type: 'auth', token: g.token }));
          scheduleRefresh(g.exp);
        } catch (e) {
          setError(`token refresh failed: ${(e as Error).message}`); // the socket closes 4001 at expiry -> reconnect
        }
      }, ms);
    };

    async function connect() {
      if (dead) return;
      let g: RobotStateGrant;
      try {
        g = await getGrant();
      } catch (e) {
        const st = e instanceof GrantError ? e.status : 0;
        retry(st === 401 ? `operator login required (${(e as Error).message})` : `no state grant: ${(e as Error).message}`);
        if (st === 401 || st === 403) backoff = 10_000; // asking faster will not log anybody in
        return;
      }
      if (dead) return;
      const url = stateUrlFrom(g);
      if (!url) {
        retry('the robot has not published a state URL (is its stream on?)');
        return;
      }
      const sock = new WebSocket(url);
      ws = sock;
      sock.onopen = () => {
        sock.send(JSON.stringify({ type: 'auth', token: g.token }));
        const ping = () => sock.readyState === WebSocket.OPEN && sock.send(JSON.stringify({ type: 'ping', c: Date.now() }));
        ping();
        pinger = setInterval(ping, 1000);
        scheduleRefresh(g.exp);
      };
      sock.onmessage = (ev) => {
        if (typeof ev.data !== 'string') return;
        const recv = Date.now();
        let m: Record<string, unknown>;
        try {
          m = JSON.parse(ev.data);
        } catch {
          return;
        }
        if (m.t === 'state') {
          const s = parseStateMessage(m);
          if (!s) return;
          deliver(toLiveState(s), s.sim, recv, clock.offsetMs == null ? null : clock.latency(s.t_ms, recv));
        } else if (m.t === 'pong' && typeof m.c === 'number' && typeof m.server_ms === 'number') {
          clock.onPong(m.c, recv, m.server_ms);
        } else if (m.t === 'hello') {
          backoff = 1000;
          setTransport('ws');
          setError(null);
          if (typeof m.server_ms === 'number') clock.onPong(recv, recv, m.server_ms); // coarse until the first pong
        } else if (m.t === 'error') {
          setError(`robot: ${String(m.code)}`);
        }
      };
      sock.onclose = (ev) => {
        if (pinger) clearInterval(pinger);
        pinger = null;
        if (ws === sock) ws = null;
        lastRx.current = 0; // stale at the next readout: a closed feed is never "fresh"
        retry(ev.code === 4001 ? 'token expired' : ev.code === 4003 ? `refused (${ev.reason || 'bad token'})` : 'state socket closed');
      };
      sock.onerror = () => {
        /* onclose follows */
      };
    }

    connect();
    return () => {
      dead = true;
      clearInterval(meter);
      if (pinger) clearInterval(pinger);
      timers.forEach(clearTimeout);
      ws?.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [grantKey, enabled, opts.fixture, staleMs]);

  const source: RobotLiveSource = opts.fixture || msgSim === true || (msgSim == null && rowSim) ? 'sim' : 'live';
  return { state, hz, latencyMs, source, stale, transport, stateRef, error, heartbeat, rttMs };
}
