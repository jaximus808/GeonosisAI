// TDM-55: browser client for the robot camera stream (transport "jpeg-ws/1").
//
// Server: services/robot-client/src/rdf_robot_client/stream.py (protocol in its docstring).
// One WebSocket carries every camera; each binary message is one JPEG with a header
// (camera, capture time on the robot's monotonic clock, camera seq). We ack every frame after it
// is drawn, so the robot never queues more than 2 frames per camera for us: on a slow link fps
// drops instead of latency growing. Ping/pong syncs clocks so latency = now - capture time.
//
// Auth: `getGrant()` returns a short-lived token (orchestrator POST /stream/token, claimant only).
// We send it as the first message and again at half-life; when the robot says it expired or
// the socket drops, we reconnect with a fresh grant (backoff 1 s .. 10 s).
//
// Fallback: if the WebSocket cannot be opened 3 times in a row (proxy, captive portal), we poll
// `GET <http>/snapshot/<cam>.jpg?token=` at ~5 fps, and say so in the state ("fallback").

export interface StreamGrant {
  token: string;
  exp: number; // unix seconds
  stream_url: string; // ws(s)://.../ws/cameras
  http_url?: string | null; // http(s)://... for snapshot fallback
  cams?: string[];
  robot_id?: number;
  task_id?: number;
}

export type FeedState = 'connecting' | 'live' | 'fallback' | 'reconnecting' | 'denied' | 'error' | 'closed';

export interface CamStats {
  fps: number; // frames drawn in the last second
  latencyMs: number | null; // capture -> on screen, median of the last second
  latencyP95Ms: number | null;
  lastFrameAt: number | null; // performance.now() of the last drawn frame
  ageMs: number | null; // how old the image on screen is now (staleness)
  kbps: number;
  width: number;
  height: number;
}

export interface FeedSnapshot {
  state: FeedState;
  detail?: string;
  cams: string[];
  stats: Record<string, CamStats>;
  rttMs: number | null;
  tokenExp: number | null;
}

type DrawTarget = (cam: string, bmp: ImageBitmap) => void;

const enc = new TextDecoder('ascii');

function parseFrame(buf: ArrayBuffer) {
  const v = new DataView(buf);
  if (v.getUint8(0) !== 1) throw new Error('unknown frame version');
  const n = v.getUint8(1);
  const cam = enc.decode(new Uint8Array(buf, 2, n));
  const off = 2 + n;
  const captureNs = v.getBigUint64(off);
  const seq = v.getUint32(off + 16);
  return { cam, captureMs: Number(captureNs / 1000n) / 1000, seq, jpeg: new Blob([new Uint8Array(buf, off + 20)], { type: 'image/jpeg' }) };
}

class Window1s {
  private xs: { t: number; v: number }[] = [];
  push(t: number, v: number) {
    this.xs.push({ t, v });
    this.trim(t);
  }
  trim(t: number) {
    while (this.xs.length && this.xs[0].t < t - 1000) this.xs.shift();
  }
  count(t: number) {
    this.trim(t);
    return this.xs.length;
  }
  sum(t: number) {
    this.trim(t);
    return this.xs.reduce((a, x) => a + x.v, 0);
  }
  pct(t: number, p: number): number | null {
    this.trim(t);
    if (!this.xs.length) return null;
    const s = this.xs.map((x) => x.v).sort((a, b) => a - b);
    return s[Math.min(s.length - 1, Math.floor(p * s.length))];
  }
}

export class CameraStreamClient {
  private ws: WebSocket | null = null;
  private stopped = false;
  private grant: StreamGrant | null = null;
  private refreshTimer: ReturnType<typeof setTimeout> | null = null;
  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private statsTimer: ReturnType<typeof setInterval> | null = null;
  private pollTimer: ReturnType<typeof setTimeout> | null = null;
  private retry = 0;
  private wsFailures = 0;
  private polling = false; // snapshot fallback running
  private offsets: { rtt: number; off: number }[] = []; // off = server_ms - client_ms
  private lat: Record<string, Window1s> = {};
  private bytes: Record<string, Window1s> = {};
  private last: Record<string, { at: number; captureLocal: number; w: number; h: number }> = {};
  private snap: FeedSnapshot = { state: 'connecting', cams: [], stats: {}, rttMs: null, tokenExp: null };

  constructor(
    private getGrant: () => Promise<StreamGrant>,
    private draw: DrawTarget,
    private onChange: (s: FeedSnapshot) => void,
    private opts: { cams?: string[]; fps?: number } = {},
  ) {}

  start() {
    this.stopped = false;
    this.statsTimer = setInterval(() => this.emit(), 250);
    void this.connect();
  }

  stop() {
    this.stopped = true;
    for (const t of [this.refreshTimer, this.pollTimer]) if (t) clearTimeout(t);
    for (const t of [this.pingTimer, this.statsTimer]) if (t) clearInterval(t);
    this.ws?.close(1000, 'bye');
    this.ws = null;
    this.set('closed');
  }

  private set(state: FeedState, detail?: string) {
    this.snap = { ...this.snap, state, detail };
    this.emit();
  }

  private emit() {
    const now = performance.now();
    const stats: Record<string, CamStats> = {};
    for (const cam of this.snap.cams) {
      const l = this.last[cam];
      stats[cam] = {
        fps: this.lat[cam]?.count(now) ?? 0,
        latencyMs: this.lat[cam]?.pct(now, 0.5) ?? null,
        latencyP95Ms: this.lat[cam]?.pct(now, 0.95) ?? null,
        lastFrameAt: l?.at ?? null,
        ageMs: l ? now - l.captureLocal : null,
        kbps: Math.round(((this.bytes[cam]?.sum(now) ?? 0) * 8) / 1000),
        width: l?.w ?? 0,
        height: l?.h ?? 0,
      };
    }
    const best = this.bestOffset();
    this.onChange({ ...this.snap, stats, rttMs: best ? Math.round(best.rtt) : null });
  }

  private bestOffset() {
    if (!this.offsets.length) return null;
    return this.offsets.reduce((a, b) => (b.rtt < a.rtt ? b : a));
  }

  private async fetchGrant(): Promise<StreamGrant | null> {
    try {
      const g = await this.getGrant();
      this.grant = g;
      this.snap = { ...this.snap, tokenExp: g.exp };
      return g;
    } catch (e) {
      const msg = (e as Error).message || 'no stream token';
      // 401/403 from the orchestrator: not (or no longer) the claimant. Don't hammer it.
      this.set(/40[13]/.test(msg) ? 'denied' : 'error', msg);
      return null;
    }
  }

  private scheduleRefresh() {
    if (this.refreshTimer) clearTimeout(this.refreshTimer);
    const g = this.grant;
    if (!g) return;
    const ms = Math.max(3000, ((g.exp - Date.now() / 1000) * 1000) / 2);
    this.refreshTimer = setTimeout(async () => {
      if (this.stopped) return;
      const ng = await this.fetchGrant();
      if (ng && this.ws?.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({ type: 'auth', token: ng.token }));
        this.scheduleRefresh();
      } else if (ng && this.snap.state === 'fallback') {
        this.scheduleRefresh();
      } else if (!ng) {
        // Lost the claim (or orchestrator down): the robot closes us at exp; retry slowly.
        this.refreshTimer = setTimeout(() => this.scheduleRefresh(), 5000);
      }
    }, ms);
  }

  private backoff(): number {
    this.retry = Math.min(this.retry + 1, 5);
    return Math.min(10_000, 500 * 2 ** this.retry);
  }

  private async connect() {
    if (this.stopped) return;
    if (!this.polling) this.set(this.retry ? 'reconnecting' : 'connecting');
    const g = await this.fetchGrant();
    if (!g) {
      if (!this.stopped) setTimeout(() => this.connect(), this.snap.state === 'denied' ? 10_000 : this.backoff());
      return;
    }
    let opened = false;
    const ws = new WebSocket(g.stream_url);
    ws.binaryType = 'arraybuffer';
    this.ws = ws;
    ws.onopen = () => {
      opened = true;
      this.wsFailures = 0;
      ws.send(JSON.stringify({ type: 'auth', token: g.token }));
      if (this.opts.cams || this.opts.fps) {
        ws.send(JSON.stringify({ type: 'subscribe', cams: this.opts.cams, fps: this.opts.fps }));
      }
      this.offsets = [];
      if (this.pingTimer) clearInterval(this.pingTimer);
      const ping = () => ws.readyState === WebSocket.OPEN && ws.send(JSON.stringify({ type: 'ping', t: performance.now() }));
      ping();
      this.pingTimer = setInterval(ping, 1000);
    };
    ws.onmessage = (ev) => {
      if (typeof ev.data === 'string') return this.onJson(JSON.parse(ev.data));
      void this.onFrame(ws, ev.data as ArrayBuffer);
    };
    ws.onclose = (ev) => {
      if (this.pingTimer) clearInterval(this.pingTimer);
      if (this.ws !== ws || this.stopped) return;
      this.ws = null;
      if (!opened) this.wsFailures += 1;
      if (this.wsFailures >= 3 && g.http_url) {
        this.startFallback();
        setTimeout(() => this.connect(), 30_000); // keep trying the WebSocket in the background
        return;
      }
      const detail = ev.code === 4003 ? `robot refused the token (${ev.reason || 'forbidden'})` : ev.code === 4001 ? 'token expired' : `socket closed (${ev.code})`;
      this.set('reconnecting', detail);
      setTimeout(() => this.connect(), ev.code === 4001 ? 200 : this.backoff());
    };
    ws.onerror = () => {
      /* onclose follows */
    };
  }

  private onJson(m: { type: string; [k: string]: unknown }) {
    if (m.type === 'hello') {
      this.retry = 0;
      this.polling = false; // WebSocket works (again): stop the snapshot fallback
      this.snap = { ...this.snap, cams: (m.cams as string[]) ?? [], tokenExp: (m.exp as number) ?? this.snap.tokenExp };
      this.set('live');
      this.scheduleRefresh();
    } else if (m.type === 'pong') {
      const t1 = performance.now();
      const t0 = m.t as number;
      this.offsets.push({ rtt: t1 - t0, off: (m.server_ns as number) / 1e6 - (t0 + t1) / 2 });
      if (this.offsets.length > 30) this.offsets.shift();
    } else if (m.type === 'auth_ok') {
      this.snap = { ...this.snap, tokenExp: m.exp as number };
    } else if (m.type === 'error') {
      this.snap = { ...this.snap, detail: String(m.code) };
    }
  }

  private async onFrame(ws: WebSocket, buf: ArrayBuffer) {
    let f: ReturnType<typeof parseFrame>;
    try {
      f = parseFrame(buf);
    } catch {
      return;
    }
    try {
      const bmp = await createImageBitmap(f.jpeg);
      this.draw(f.cam, bmp);
      this.record(f.cam, f.captureMs, buf.byteLength, bmp.width, bmp.height);
      bmp.close();
    } finally {
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'ack', cam: f.cam, seq: f.seq }));
    }
  }

  private record(cam: string, captureServerMs: number | null, bytes: number, w: number, h: number) {
    const now = performance.now();
    const best = this.bestOffset();
    const captureLocal = captureServerMs != null && best ? captureServerMs - best.off : now;
    (this.lat[cam] ??= new Window1s()).push(now, now - captureLocal);
    (this.bytes[cam] ??= new Window1s()).push(now, bytes);
    this.last[cam] = { at: now, captureLocal, w, h };
    if (!this.snap.cams.includes(cam)) this.snap = { ...this.snap, cams: [...this.snap.cams, cam] };
  }

  // -- snapshot polling fallback (no clock sync: latency shown is fetch time, an underestimate of age)
  private startFallback() {
    const g = this.grant;
    if (!g?.http_url || this.polling) return;
    this.polling = true;
    const cams = this.opts.cams ?? (g.cams?.length ? g.cams : this.snap.cams.length ? this.snap.cams : ['top', 'wrist']);
    this.snap = { ...this.snap, cams };
    this.set('fallback', 'WebSocket blocked: polling snapshots (~5 fps)');
    this.scheduleRefresh();
    const tick = async () => {
      if (this.stopped || !this.polling) return;
      const gg = this.grant!;
      await Promise.all(
        cams.map(async (cam) => {
          const t0 = performance.now();
          try {
            const r = await fetch(`${gg.http_url}/snapshot/${cam}.jpg?token=${encodeURIComponent(gg.token)}`, { cache: 'no-store' });
            if (!r.ok) return;
            const blob = await r.blob();
            const bmp = await createImageBitmap(blob);
            this.draw(cam, bmp);
            const now = performance.now();
            (this.lat[cam] ??= new Window1s()).push(now, now - t0);
            (this.bytes[cam] ??= new Window1s()).push(now, blob.size);
            this.last[cam] = { at: now, captureLocal: t0, w: bmp.width, h: bmp.height };
            bmp.close();
          } catch {
            /* keep polling */
          }
        }),
      );
      this.pollTimer = setTimeout(tick, 200);
    };
    void tick();
  }
}
