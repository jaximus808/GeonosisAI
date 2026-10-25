// Reset arm + Exit for the teleop page (components/teleop/ArmSessionButtons.tsx). Pure: tested in
// test/arm-session.test.ts.
//
// browser -> robot  {t:"reset_arm", req}      slow joint-space ramp back to the rest pose (robot-client teleop.py)
// robot -> browser  {t:"ack", op:"reset_arm", req, ok, code?, detail?, percent, seconds}   when it ends
//                   {t:"state", ..., resetting, reset:{active, percent}}                    progress, ~10 Hz
//
// Exit: (recording? discard or finish it first) -> reset_arm and wait (timeout) -> give back control -> the task
// list. A dropped socket or a lapsed claim never moves the arm (the robot just holds): only Exit / Reset do.

import type { ControlChannel, RobotControlMessage } from './tdm58Recording';

/** Where Exit lands: the task list (this app has no /tasks route; the marketplace is the list of tasks). */
export const EXIT_HREF = '/marketplace';
/** The robot plans max(5 s, 1.5 x distance / 0.2 rad/s) plus up to 3 s settling: a full fold is well under this. */
export const RESET_TIMEOUT_MS = 90_000;
export const RECORD_OP_TIMEOUT_MS = 15_000;

export interface ResetProgress {
  resetting: boolean;
  percent: number | null;
}

/** `resetting` / `reset.percent` from a robot state message (raw), or not resetting. */
export function resetProgress(raw: Record<string, unknown> | null | undefined): ResetProgress {
  if (!raw) return { resetting: false, percent: null };
  const reset = (raw.reset ?? null) as { active?: unknown; percent?: unknown } | null;
  const resetting = raw.resetting === true || reset?.active === true;
  const p = typeof reset?.percent === 'number' && Number.isFinite(reset.percent) ? Math.max(0, Math.min(100, Math.round(reset.percent))) : null;
  return { resetting, percent: resetting ? (p ?? 0) : p };
}

export function resetLabel(p: ResetProgress): string {
  return p.resetting ? `Resetting… ${p.percent ?? 0}%` : 'Reset arm';
}

/** The recording in a state message is live (exiting must discard or finish it first). */
export function recordingActive(recording: unknown): boolean {
  return !!recording && typeof recording === 'object' && (recording as { active?: unknown }).active === true;
}

export interface ResetResult {
  ok: boolean;
  code?: string;
  detail?: string;
  percent?: number;
}

const RESET_TEXT: Record<string, string> = {
  recording: 'Finish or discard the recording first.',
  clutched: 'Let go of the controls first.',
  stopped: 'The arm is stopped. A person at the robot has to clear the stop.',
  operator_gone: 'The connection dropped, so the arm stopped where it was.',
  not_arrived: 'The arm stopped before the rest pose (a safety limit). It is holding still.',
  already_resetting: 'The arm is already resetting.',
  busy: 'The robot is busy with something else. Try again in a moment.',
  takeover: 'Hand control back to the policy first.',
  target_out_of_limits: 'The rest pose is outside the safe joint range, so the robot refused it.',
  not_active: 'Another page of yours holds the controls. Close the other tab and try again.',
  lease_lost: 'You no longer hold control of the robot.',
  not_connected: 'Not connected to the robot.',
  not_allowed: 'You do not hold control right now.',
  timeout: 'No answer from the robot.',
};

export function resetErrorText(r: ResetResult): string {
  return RESET_TEXT[r.code ?? ''] ?? r.detail ?? `The robot refused it (${r.code ?? 'no reason given'}).`;
}

let reqSeq = 0;
export function armReq(prefix: string): string {
  reqSeq += 1;
  return `${prefix}-${Date.now().toString(36)}-${reqSeq}`;
}

type Timers = { setTimeout: (fn: () => void, ms: number) => unknown; clearTimeout: (h: unknown) => void };
const realTimers: Timers = {
  setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
  clearTimeout: (h) => globalThis.clearTimeout(h as ReturnType<typeof setTimeout>),
};

/** Send one op and resolve with its reply ({t:"ack"} or {t:"record_ack"} with the same req), or a timeout. */
export function sendAndWait(
  channel: ControlChannel,
  msg: Record<string, unknown> & { t: string },
  opts: { timeoutMs: number; onMessage?: (m: RobotControlMessage) => void; timers?: Timers },
): Promise<ResetResult & { reply?: RobotControlMessage }> {
  const timers = opts.timers ?? realTimers;
  const req = String(msg.req ?? armReq(msg.t));
  return new Promise((resolve) => {
    let done = false;
    let unsub: () => void = () => {};
    let timer: unknown = null;
    const finish = (r: ResetResult & { reply?: RobotControlMessage }) => {
      if (done) return;
      done = true;
      if (timer != null) timers.clearTimeout(timer);
      unsub();
      resolve(r);
    };
    unsub = channel.subscribe((m) => {
      opts.onMessage?.(m);
      if ((m.t === 'ack' || m.t === 'record_ack') && m.op === msg.t && (m.req === req || m.req == null)) {
        const ok = m.ok !== false;
        finish({
          ok,
          code: typeof m.code === 'string' ? m.code : ok ? undefined : 'refused',
          detail: typeof m.detail === 'string' ? m.detail : typeof m.error === 'string' ? m.error : undefined,
          percent: typeof m.percent === 'number' ? m.percent : undefined,
          reply: m,
        });
      }
    });
    timer = timers.setTimeout(() => finish({ ok: false, code: 'timeout' }), opts.timeoutMs);
    if (!channel.send({ ...msg, req })) finish({ ok: false, code: 'not_allowed' });
  });
}

/** {t:"reset_arm"}: resolves when the robot says the ramp ended (ok) or refused / aborted it, or on timeout. */
export function requestReset(
  channel: ControlChannel,
  opts: { timeoutMs?: number; onProgress?: (p: ResetProgress) => void; timers?: Timers } = {},
): Promise<ResetResult> {
  return sendAndWait(channel, { t: 'reset_arm', req: armReq('reset') }, {
    timeoutMs: opts.timeoutMs ?? RESET_TIMEOUT_MS,
    timers: opts.timers,
    onMessage: (m) => {
      if (m.t === 'state') opts.onProgress?.(resetProgress(m));
    },
  });
}

export type ExitStep =
  | { phase: 'recording'; action: 'discard' | 'finish' }
  | { phase: 'resetting'; percent: number }
  | { phase: 'releasing' }
  | { phase: 'leaving' };

export interface ExitDeps {
  channel: ControlChannel;
  /** Give back control (TDM-57 lease.release: release_task + /texts/release). */
  release: () => Promise<void>;
  navigate: (href: string) => void;
  onStep?: (s: ExitStep) => void;
  /** What to do with a live recording (the page asked "Discard this recording?" first). */
  recording?: 'discard' | 'finish' | null;
  /** Leave even when the reset failed (the operator chose "Leave anyway"). */
  leaveOnResetFailure?: boolean;
  timeoutMs?: number;
  timers?: Timers;
}

/** Exit, in order: end the recording if asked, reset_arm and wait, give back control, go to the task list.
 *  Resolves with the reset result; when the reset fails (and not leaveOnResetFailure) it stops there. */
export async function runExit(d: ExitDeps): Promise<ResetResult & { left: boolean }> {
  if (d.recording) {
    d.onStep?.({ phase: 'recording', action: d.recording });
    const msg = d.recording === 'discard'
      ? { t: 'record_discard', reason: 'operator exited the session' }
      : { t: 'record_stop', success: true };
    const r = await sendAndWait(d.channel, { ...msg, req: armReq('exit') }, { timeoutMs: RECORD_OP_TIMEOUT_MS, timers: d.timers });
    if (!r.ok && !d.leaveOnResetFailure) return { ...r, left: false };
  }
  d.onStep?.({ phase: 'resetting', percent: 0 });
  const r = await requestReset(d.channel, {
    timeoutMs: d.timeoutMs,
    timers: d.timers,
    onProgress: (p) => d.onStep?.({ phase: 'resetting', percent: p.percent ?? 0 }),
  });
  if (!r.ok && !d.leaveOnResetFailure) return { ...r, left: false };
  await leave(d);
  return { ...r, left: true };
}

/** Give back control (best effort) and go to the task list. */
export async function leave(d: Pick<ExitDeps, 'release' | 'navigate' | 'onStep'>): Promise<void> {
  d.onStep?.({ phase: 'releasing' });
  try {
    await d.release();
  } catch {
    /* the claim lapses on its own; the robot holds meanwhile */
  }
  d.onStep?.({ phase: 'leaving' });
  d.navigate(EXIT_HREF);
}
