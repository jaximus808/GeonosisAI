// TDM-56: browser side of the teleop `/control` WebSocket (contract: docs/WORKER_GUIDE.md, "Teleop contract").
//
// Same stream server, tunnel and token as the TDM-55 camera feed. URL: the grant's `control_url`
// if the robot side ever adds one, else the camera URL with `/ws/cameras` swapped for `/ws/control`.
//
//   browser -> robot   {t:"cmd", seq, ts_ms, clutch, d_pos:[m], d_rot:[rad axis-angle], gripper:0..1, speed_scale:0..1}
//                      {t:"stop"} | {t:"handback"} | {t:"takeover"} | {t:"record_start"} | {t:"record_stop", success?}
//   robot -> browser   {t:"state", ...} ~10 Hz, {t:"ack", seq}
//
// Auth: the first message is {type:"auth", t:"auth", token} (both keys, so it matches the camera
// socket's `type` convention and the control contract's `t` one), resent at the token's half-life.
// Reconnects with a fresh grant (backoff 0.5 s .. 5 s). Never sends motion on its own: the console
// decides when to call sendCmd().

import type { StreamGrant } from './tdm55CameraClient';

export type Tdm56Vec3 = [number, number, number];

export interface Tdm56Cmd {
  t: 'cmd';
  seq: number;
  ts_ms: number;
  clutch: boolean;
  d_pos: Tdm56Vec3;
  d_rot: Tdm56Vec3;
  gripper: number;
  speed_scale: number;
}

export type Tdm56OutMsg =
  | Tdm56Cmd
  | { t: 'stop' }
  | { t: 'handback' }
  | { t: 'takeover' }
  | { t: 'record_start' }
  | { t: 'record_stop'; success?: boolean }
  | { t: 'record_discard'; reason?: string };

/** What we understood from the robot's {t:"state"} message (raw kept for display/debug). */
export interface Tdm56RobotState {
  at: number; // performance.now() when received
  pos?: Tdm56Vec3; // EE position, m (base frame)
  rot?: number[]; // quaternion [x,y,z,w] or rpy, as sent
  rotKind?: 'quat' | 'rpy';
  joints?: number[];
  gripper?: number;
  controlSource?: string;
  recording?: unknown;
  stopped?: boolean; // latched soft stop
  watchdog?: boolean;
  clamps: string[]; // safety clamps active / recently hit
  speedScale?: number;
  raw: Record<string, unknown>;
}

export type Tdm56LinkState = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'denied' | 'error' | 'closed';

export interface Tdm56LinkSnapshot {
  state: Tdm56LinkState;
  detail?: string;
  url?: string;
  /** cmd messages sent in the last second */
  cmdHz: number;
  /** median cmd -> ack round trip over the last ~30 acks */
  ackRttMs: number | null;
  lastAckAt: number | null;
  lastStateAt: number | null;
  robot: Tdm56RobotState | null;
  /** STOP pressed while the link was down: we keep retrying it on reconnect. */
  stopPending: boolean;
}

const num = (x: unknown): number | undefined => (typeof x === 'number' && Number.isFinite(x) ? x : undefined);
const nums = (x: unknown): number[] | undefined =>
  Array.isArray(x) && x.every((v) => typeof v === 'number') ? (x as number[]) : undefined;

/** Tolerant parse of the robot's state message; TDM-52 owns the exact field names. */
export function tdm56ParseState(m: Record<string, unknown>): Tdm56RobotState {
  const pose = (m.pose ?? m.ee_pose ?? m.ee) as Record<string, unknown> | number[] | undefined;
  let pos: number[] | undefined;
  let rot: number[] | undefined;
  let rotKind: 'quat' | 'rpy' | undefined;
  if (Array.isArray(pose)) {
    pos = pose.slice(0, 3) as number[];
    if (pose.length >= 7) {
      rot = pose.slice(3, 7) as number[];
      rotKind = 'quat';
    } else if (pose.length >= 6) {
      rot = pose.slice(3, 6) as number[];
      rotKind = 'rpy';
    }
  } else if (pose && typeof pose === 'object') {
    pos = nums(pose.pos ?? pose.xyz ?? pose.position);
    const q = nums(pose.quat ?? pose.q ?? pose.orientation);
    const r = nums(pose.rpy ?? pose.rot);
    if (q && q.length === 4) {
      rot = q;
      rotKind = 'quat';
    } else if (r) {
      rot = r;
      rotKind = 'rpy';
    }
  }
  pos ??= nums(m.ee_pos ?? m.pos);
  const clampsRaw = m.clamps ?? m.safety ?? m.limits;
  let clamps: string[] = [];
  if (Array.isArray(clampsRaw)) clamps = clampsRaw.map(String);
  else if (clampsRaw && typeof clampsRaw === 'object') {
    // {clamped_workspace: true} or {clamped_workspace: 3} (only report active/non-zero)
    clamps = Object.entries(clampsRaw as Record<string, unknown>)
      .filter(([, v]) => v === true || (typeof v === 'number' && v > 0) || (Array.isArray(v) && v.length > 0))
      .map(([k]) => k);
  }
  const gr = m.gripper;
  const gripper = num(gr) ?? (gr && typeof gr === 'object' ? num((gr as Record<string, unknown>).pos) : undefined);
  return {
    at: performance.now(),
    pos: pos && pos.length >= 3 ? [pos[0], pos[1], pos[2]] : undefined,
    rot,
    rotKind,
    joints: nums(m.joints ?? m.joint_pos),
    gripper: gripper ?? num(m.gripper_pos),
    controlSource: typeof m.control_source === 'string' ? m.control_source : undefined,
    recording: m.recording ?? m.recording_state,
    stopped: Boolean(m.stopped ?? m.estop ?? m.soft_stop ?? m.locked),
    watchdog: Boolean(m.watchdog ?? m.watchdog_hold),
    clamps,
    speedScale: num(m.speed_scale),
    raw: m,
  };
}

export function tdm56ControlUrl(g: StreamGrant & { control_url?: string | null }, override?: string | null): string {
  if (override) return override;
  if (g.control_url) return g.control_url;
  const u = g.stream_url;
  if (/\/ws\/cameras(\?.*)?$/.test(u)) return u.replace(/\/ws\/cameras(\?.*)?$/, '/ws/control');
  return u.replace(/\/?$/, '/ws/control');
}

export class Tdm56ControlClient {
  private ws: WebSocket | null = null;
  private stopped = false;
  private retry = 0;
  private grant: StreamGrant | null = null;
  private refreshTimer: ReturnType<typeof setTimeout> | null = null;
  private statsTimer: ReturnType<typeof setInterval> | null = null;
  private sentAt = new Map<number, number>();
  private rtts: number[] = [];
  private cmdTimes: number[] = [];
  private subs = new Set<(m: Record<string, unknown> & { t: string }) => void>();
  private snap: Tdm56LinkSnapshot = {
    state: 'idle',
    cmdHz: 0,
    ackRttMs: null,
    lastAckAt: null,
    lastStateAt: null,
    robot: null,
    stopPending: false,
  };

  constructor(
    private getGrant: () => Promise<StreamGrant>,
    private onChange: (s: Tdm56LinkSnapshot) => void,
    private opts: { urlOverride?: string | null } = {},
  ) {}

  get isOpen() {
    return this.ws?.readyState === WebSocket.OPEN && this.snap.state === 'open';
  }

  start() {
    this.stopped = false;
    this.statsTimer = setInterval(() => this.emit(), 250);
    void this.connect();
  }

  stop() {
    this.stopped = true;
    if (this.refreshTimer) clearTimeout(this.refreshTimer);
    if (this.statsTimer) clearInterval(this.statsTimer);
    this.ws?.close(1000, 'bye');
    this.ws = null;
    this.set('closed');
  }

  /** Motion command. Returns false (and sends nothing) if the link is not open. */
  sendCmd(c: Omit<Tdm56Cmd, 't' | 'ts_ms'>): boolean {
    if (!this.isOpen) return false;
    const now = performance.now();
    const msg: Tdm56Cmd = { t: 'cmd', ts_ms: Date.now(), ...c };
    this.ws!.send(JSON.stringify(msg));
    this.sentAt.set(c.seq, now);
    if (this.sentAt.size > 200) this.sentAt.delete(this.sentAt.keys().next().value!);
    this.cmdTimes.push(now);
    return true;
  }

  /** Soft e-stop. Sent at once (and twice more 80 ms apart); if the link is down, re-sent on reconnect. */
  sendStop(): boolean {
    const ok = this.sendRaw({ t: 'stop' });
    if (ok) {
      for (const d of [80, 160]) setTimeout(() => this.sendRaw({ t: 'stop' }), d);
    }
    this.snap = { ...this.snap, stopPending: !ok };
    this.emit();
    return ok;
  }

  /** Any other contract message (TDM-58 recording, takeover/handback). */
  send(msg: Exclude<Tdm56OutMsg, Tdm56Cmd>): boolean {
    if (msg.t === 'stop') return this.sendStop();
    return this.sendRaw(msg);
  }

  /** Any JSON message (TDM-58 recording controls add `req` / `note` / `reason`). `{t:"stop"}` goes through sendStop. */
  sendJson(msg: Record<string, unknown>): boolean {
    if (msg.t === 'stop') return this.sendStop();
    return this.sendRaw(msg);
  }

  /** Every robot -> browser message, parsed (state, ack, record_ack, lease_lost, ...). Returns an unsubscribe. */
  subscribe(fn: (m: Record<string, unknown> & { t: string }) => void): () => void {
    this.subs.add(fn);
    return () => {
      this.subs.delete(fn);
    };
  }

  private sendRaw(msg: object): boolean {
    if (!this.isOpen) return false;
    this.ws!.send(JSON.stringify(msg));
    return true;
  }

  private set(state: Tdm56LinkState, detail?: string) {
    this.snap = { ...this.snap, state, detail };
    this.emit();
  }

  private emit() {
    const now = performance.now();
    while (this.cmdTimes.length && this.cmdTimes[0] < now - 1000) this.cmdTimes.shift();
    const s = [...this.rtts].sort((a, b) => a - b);
    this.onChange({ ...this.snap, cmdHz: this.cmdTimes.length, ackRttMs: s.length ? Math.round(s[s.length >> 1]) : null });
  }

  private scheduleRefresh() {
    if (this.refreshTimer) clearTimeout(this.refreshTimer);
    const g = this.grant;
    if (!g) return;
    const ms = Math.max(3000, ((g.exp - Date.now() / 1000) * 1000) / 2);
    this.refreshTimer = setTimeout(async () => {
      if (this.stopped) return;
      try {
        const ng = await this.getGrant();
        this.grant = ng;
        this.sendRaw({ type: 'auth', t: 'auth', token: ng.token });
        this.scheduleRefresh();
      } catch {
        // Lost the claim or the orchestrator is down: the robot closes us at exp. Retry slowly.
        this.refreshTimer = setTimeout(() => this.scheduleRefresh(), 5000);
      }
    }, ms);
  }

  private async connect() {
    if (this.stopped) return;
    this.set(this.retry ? 'reconnecting' : 'connecting');
    let g: StreamGrant;
    try {
      g = await this.getGrant();
      this.grant = g;
    } catch (e) {
      const msg = (e as Error).message || 'no control token';
      const denied = /40[13]/.test(msg);
      this.set(denied ? 'denied' : 'error', msg);
      if (!this.stopped) setTimeout(() => this.connect(), denied ? 10_000 : this.backoff());
      return;
    }
    const url = tdm56ControlUrl(g, this.opts.urlOverride);
    this.snap = { ...this.snap, url };
    const ws = new WebSocket(url);
    this.ws = ws;
    ws.onopen = () => {
      ws.send(JSON.stringify({ type: 'auth', t: 'auth', token: g.token }));
      this.retry = 0;
      this.rtts = [];
      this.set('open');
      this.scheduleRefresh();
      if (this.snap.stopPending) this.sendStop();
    };
    ws.onmessage = (ev) => {
      if (typeof ev.data !== 'string') return;
      let m: Record<string, unknown>;
      try {
        m = JSON.parse(ev.data);
      } catch {
        return;
      }
      const kind = (m.t ?? m.type) as string | undefined;
      const now = performance.now();
      if (kind === 'state') {
        this.snap = { ...this.snap, robot: tdm56ParseState(m), lastStateAt: now };
      } else if (kind === 'ack') {
        const seq = num(m.seq);
        const t0 = seq != null ? this.sentAt.get(seq) : undefined;
        if (t0 != null) {
          this.rtts.push(now - t0);
          if (this.rtts.length > 30) this.rtts.shift();
          this.sentAt.delete(seq!);
        }
        this.snap = { ...this.snap, lastAckAt: now };
      } else if (kind === 'error') {
        this.snap = { ...this.snap, detail: String(m.code ?? m.detail ?? 'robot error') };
      }
      if (kind && this.subs.size) {
        const msg = { ...m, t: kind };
        for (const fn of this.subs) {
          try {
            fn(msg);
          } catch {
            /* a subscriber's bug must not break the control link */
          }
        }
      }
    };
    ws.onclose = (ev) => {
      if (this.ws !== ws || this.stopped) return;
      this.ws = null;
      const detail =
        ev.code === 4003 ? `robot refused the token (${ev.reason || 'forbidden'})` : ev.code === 4001 ? 'token expired' : `link closed (${ev.code})`;
      this.set('reconnecting', detail);
      setTimeout(() => this.connect(), ev.code === 4001 ? 200 : this.backoff());
    };
    ws.onerror = () => {
      /* onclose follows */
    };
  }

  private backoff(): number {
    this.retry = Math.min(this.retry + 1, 4);
    return Math.min(5000, 250 * 2 ** this.retry);
  }
}
