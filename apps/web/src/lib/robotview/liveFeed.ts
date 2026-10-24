// TDM-96: the browser side of the robot's `/ws/state` feed (protocol `rdf-state/1`), without React.
//
// Server: services/robot-client/src/rdf_robot_client/stream.py (LiveStateSampler / StateFeed / state_ws).
//   client -> robot  {type:'auth', token} first and at half-life; {type:'ping', c:<client ms>} every second
//   robot -> client  {t:'hello', protocol, robot_id, hz, sim, exp, scope, server_ms}
//                    {t:'state', v:1, sim, ...RobotLiveState}   ~30 Hz, newest only
//                    {t:'pong', c, server_ms} / {t:'auth_ok', exp} / {t:'error', code}
//   close 4001 = token expired, 4003 = bad token
//
// Pure pieces (unit-tested in test/robotview-live.test.ts): RateMeter (messages in the last second),
// ClockSync (robot wall clock vs ours from ping/pong, min-RTT sample wins), parseStateMessage, and
// the fixture player. useRobotLive.ts wires them to a WebSocket and React.

import type { RobotLiveState, RobotStateMessage } from './types';

export const STATE_PROTOCOL = 'rdf-state/1';
/** No state for this long = stale. Checked at 10 Hz, so `stale` shows < 1 s after the robot client dies. */
export const STALE_MS = 700;

/** Messages per second over a sliding window. */
export class RateMeter {
  private times: number[] = [];
  private windowMs: number;
  constructor(windowMs = 1000) {
    this.windowMs = windowMs;
  }
  tick(nowMs: number): void {
    this.times.push(nowMs);
    this.trim(nowMs);
  }
  hz(nowMs: number): number {
    this.trim(nowMs);
    return (this.times.length * 1000) / this.windowMs;
  }
  private trim(nowMs: number): void {
    const cut = nowMs - this.windowMs;
    let i = 0;
    while (i < this.times.length && this.times[i] <= cut) i++;
    if (i) this.times.splice(0, i);
  }
}

/**
 * Robot clock offset from ping/pong: offset = server_ms - midpoint(client send, client receive).
 * Keeps the last `keep` samples and trusts the one with the smallest round trip.
 */
export class ClockSync {
  private samples: { rtt: number; offset: number }[] = [];
  private keep: number;
  constructor(keep = 20) {
    this.keep = keep;
  }
  onPong(clientSentMs: number, clientRecvMs: number, serverMs: number): void {
    const rtt = clientRecvMs - clientSentMs;
    if (!(rtt >= 0) || !Number.isFinite(serverMs)) return;
    this.samples.push({ rtt, offset: serverMs - (clientSentMs + clientRecvMs) / 2 });
    if (this.samples.length > this.keep) this.samples.shift();
  }
  /** robot clock - our clock, ms (null until the first pong). */
  get offsetMs(): number | null {
    if (!this.samples.length) return null;
    return this.samples.reduce((a, b) => (b.rtt < a.rtt ? b : a)).offset;
  }
  get rttMs(): number | null {
    return this.samples.length ? Math.min(...this.samples.map((s) => s.rtt)) : null;
  }
  /** Sample -> receive latency of a state stamped `tMs` on the robot clock, received at `recvMs` (ours). */
  latency(tMs: number, recvMs: number): number {
    return recvMs - (tMs - (this.offsetMs ?? 0));
  }
}

/** Median of the last `n` values (latency readout without jitter). */
export class RollingMedian {
  private xs: number[] = [];
  private n: number;
  constructor(n = 30) {
    this.n = n;
  }
  push(x: number): void {
    this.xs.push(x);
    if (this.xs.length > this.n) this.xs.shift();
  }
  get value(): number | null {
    if (!this.xs.length) return null;
    const s = [...this.xs].sort((a, b) => a - b);
    return s[Math.floor(s.length / 2)];
  }
}

function isNumArr(v: unknown, len?: number): v is number[] {
  return Array.isArray(v) && (len == null || v.length === len) && v.every((x) => typeof x === 'number' && Number.isFinite(x));
}

/** A `{t:'state'}` message checked against the contract, or null (wrong shape: ignore it, never draw it). */
export function parseStateMessage(m: unknown): RobotStateMessage | null {
  if (!m || typeof m !== 'object') return null;
  const o = m as Record<string, unknown>;
  if (o.t !== 'state' || typeof o.t_ms !== 'number') return null;
  const ee = o.ee as Record<string, unknown> | undefined;
  if (!isNumArr(o.joints) || typeof o.gripper !== 'number' || !ee || !isNumArr(ee.pos, 3) || !isNumArr(ee.quat, 4)) return null;
  const cs = o.control_source;
  const safety = (o.safety as { clamps_1s?: Record<string, number> } | undefined) ?? {};
  return {
    ...(o as unknown as RobotStateMessage),
    v: 1,
    sim: o.sim === true,
    control_source: cs === 'policy' || cs === 'human' ? cs : 'idle',
    robot_state: typeof o.robot_state === 'string' ? o.robot_state : 'unknown',
    estop: o.estop === true,
    safety: { clamps_1s: safety.clamps_1s && typeof safety.clamps_1s === 'object' ? safety.clamps_1s : {} },
  };
}

/** Drop the wire-only keys so consumers get exactly a RobotLiveState. */
export function toLiveState(m: RobotStateMessage): RobotLiveState {
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  const { t, v, sim, ...rest } = m as RobotStateMessage & { t: string; v: number; sim: boolean };
  return rest;
}

/** `/ws/state` from a grant, or from a camera URL (`.../ws/cameras` -> `.../ws/state`). */
export function stateUrlFrom(grant: { state_url?: string | null; stream_url?: string | null }): string | null {
  if (grant.state_url) return grant.state_url;
  const u = grant.stream_url;
  if (u && /\/ws\/cameras$/.test(u)) return u.replace(/\/ws\/cameras$/, '/ws/state');
  return null;
}

// ------------------------------------------------------------------------------- recorded fixture

/** apps/web/src/lib/robotview/fixtures/*.json: a recording of a real `/ws/state` session. */
export interface RobotStateFixture {
  fixture: true;
  label: string;
  recorded_from: string;
  recorded_at: string;
  hz: number;
  frames: RobotStateMessage[];
}

/**
 * The fixture frame for wall time `nowMs`, looping, with `t_ms` re-stamped to now so latency reads ~0.
 * Fixture data is a simulation recording: callers must label it SIMULATED (useRobotLive reports source 'sim').
 */
export function fixtureFrameAt(fx: RobotStateFixture, startMs: number, nowMs: number): RobotStateMessage {
  const n = fx.frames.length;
  const t0 = fx.frames[0].t_ms;
  const span = fx.frames[n - 1].t_ms - t0 + 1000 / fx.hz;
  const want = t0 + ((nowMs - startMs) % span);
  let lo = 0;
  let hi = n - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (fx.frames[mid].t_ms <= want) lo = mid;
    else hi = mid - 1;
  }
  return { ...fx.frames[lo], t_ms: nowMs };
}
