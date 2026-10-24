// TDM-58 dev harness only: a bare /control WebSocket client implementing ControlChannel, so the
// recording controls can be driven before TDM-56's teleop page (which owns the real /control client,
// clutch and 30 Hz cmd stream) lands. Auth = first message {type:"auth", token} (TDM-55 stream token).

import type { ControlChannel, RobotControlMessage } from './tdm58Recording';

export class Tdm58DevControlSocket implements ControlChannel {
  private ws: WebSocket | null = null;
  private subs = new Set<(m: RobotControlMessage) => void>();
  private stopped = false;
  private backoff = 1000;
  private timer: number | null = null;

  constructor(
    private url: string,
    private token: string,
  ) {}

  start() {
    this.stopped = false;
    this.open();
    return this;
  }

  stop() {
    this.stopped = true;
    if (this.timer != null) window.clearTimeout(this.timer);
    this.ws?.close();
    this.ws = null;
  }

  send(msg: Record<string, unknown>): boolean {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return false;
    this.ws.send(JSON.stringify(msg));
    return true;
  }

  subscribe(fn: (m: RobotControlMessage) => void) {
    this.subs.add(fn);
    return () => {
      this.subs.delete(fn);
    };
  }

  private open() {
    const ws = new WebSocket(this.url);
    this.ws = ws;
    ws.onopen = () => {
      this.backoff = 1000;
      ws.send(JSON.stringify({ type: 'auth', token: this.token }));
    };
    ws.onmessage = (ev) => {
      if (typeof ev.data !== 'string') return;
      let m: RobotControlMessage;
      try {
        m = JSON.parse(ev.data);
      } catch {
        return;
      }
      for (const fn of this.subs) fn(m);
    };
    ws.onclose = () => {
      if (this.ws === ws) this.ws = null;
      if (this.stopped) return;
      this.timer = window.setTimeout(() => this.open(), this.backoff);
      this.backoff = Math.min(this.backoff * 2, 10000);
    };
  }
}
