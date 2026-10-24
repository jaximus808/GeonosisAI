// TDM-106: easier driving for first-time operators, shared by the teleop console (TDM-56) and
// practice mode (TDM-59). Pure logic, no runtime imports (node --test loads it by URL).
//
//   - Keys drive directly: holding a move key IS the clutch (no Space needed). Let go = hold still.
//     Space / RT / the on-screen button still work as a clutch for sticks and gamepads.
//   - Translation only by default (W A S D Q E). Turning the hand (arrow keys, right stick X,
//     d-pad) is behind a toggle.
//   - Slow default speed (35%) with a smooth ramp: velocity eases in over ~0.35 s instead of
//     jumping, and eases out fast. Hold Shift for precise moves (x0.35).
//   - Gripper on one key: G toggles open/closed (or, in "hold" mode, hold G to close). The robot
//     applies the gripper only while the clutch is held (teleop.py), so a gripper change engages the
//     clutch for a short "gripper pulse" with zero motion: the arm holds still, the jaws move.
//   - Snap to grasp (optional, labelled): near the object, sideways motion is pulled toward its
//     centre, so lowering onto it lines up by itself. Needs the object pose (sim scenes only).
//
// Axes are the contract's robot BASE frame: +x forward (away from the base), +y left, +z up.

import type { Tdm56Twist, V3 } from './tdm56Input';

export const TDM106_DEFAULT_SPEED = 0.35;
export const TDM106_SPEED_PRESETS = [
  { id: 'slow', label: 'Slow', speed: 0.2 },
  { id: 'normal', label: 'Normal', speed: 0.35 },
  { id: 'fast', label: 'Fast', speed: 0.7 },
] as const;
/** Shift held: precise moves at this fraction of the speed. */
export const TDM106_PRECISE = 0.35;
/** Seconds for a key's velocity to ease from 0 to full (rise) and from full to 0 (fall). */
export const TDM106_RAMP_RISE_S = 0.35;
export const TDM106_RAMP_FALL_S = 0.1;
/** How long a gripper change holds the clutch (ms), so the robot gets the new gripper value. */
export const TDM106_GRIP_PULSE_MS = 900;

export type Tdm106GripMode = 'toggle' | 'hold';

/** Translation keys (KeyboardEvent.code) -> base-frame axis, sign, and what the user sees. */
export const TDM106_MOVE_KEYS: Record<string, { i: 0 | 1 | 2; s: 1 | -1; cap: string; word: string }> = {
  KeyW: { i: 0, s: 1, cap: 'W', word: 'forward' },
  KeyS: { i: 0, s: -1, cap: 'S', word: 'back' },
  KeyA: { i: 1, s: 1, cap: 'A', word: 'left' },
  KeyD: { i: 1, s: -1, cap: 'D', word: 'right' },
  KeyQ: { i: 2, s: 1, cap: 'Q', word: 'up' },
  KeyE: { i: 2, s: -1, cap: 'E', word: 'down' },
};

/** Rotation keys, only while "Turn the hand" is on. Same mapping as TDM-56 (Shift + left/right = roll). */
export const TDM106_TURN_KEYS: Record<string, { i: 0 | 1 | 2; s: 1 | -1; cap: string; word: string }> = {
  ArrowLeft: { i: 2, s: 1, cap: '←', word: 'turn left' },
  ArrowRight: { i: 2, s: -1, cap: '→', word: 'turn right' },
  ArrowUp: { i: 1, s: -1, cap: '↑', word: 'tilt down' },
  ArrowDown: { i: 1, s: 1, cap: '↓', word: 'tilt up' },
};

export const TDM106_GRIP_KEY = 'KeyG';

/** Every key the consoles listen to (preventDefault + legend). */
export const TDM106_KEYS = new Set<string>([
  ...Object.keys(TDM106_MOVE_KEYS),
  ...Object.keys(TDM106_TURN_KEYS),
  TDM106_GRIP_KEY,
  'KeyR',
  'KeyF',
]);

const clamp1 = (v: number) => Math.max(-1, Math.min(1, v));

/** Velocity request from held keys. Rotation keys count only when `rotation` is on. */
export function tdm106KeyTwist(held: ReadonlySet<string>, opts: { rotation: boolean; shift: boolean }): Tdm56Twist {
  const t: Tdm56Twist = { lin: [0, 0, 0], ang: [0, 0, 0] };
  for (const code of held) {
    const m = TDM106_MOVE_KEYS[code];
    if (m) t.lin[m.i] = clamp1(t.lin[m.i] + m.s);
    const r = opts.rotation ? TDM106_TURN_KEYS[code] : undefined;
    if (r) {
      const i = opts.shift && (code === 'ArrowLeft' || code === 'ArrowRight') ? 0 : r.i; // roll
      t.ang[i] = clamp1(t.ang[i] + r.s);
    }
  }
  if (opts.shift) {
    t.lin = t.lin.map((v) => v * TDM106_PRECISE) as V3;
    t.ang = t.ang.map((v) => v * TDM106_PRECISE) as V3;
  }
  return t;
}

/** True when a held key should drive the arm by itself (keys-drive-directly mode). */
export function tdm106KeysWantDrive(held: ReadonlySet<string>, rotation: boolean): boolean {
  for (const code of held) {
    if (TDM106_MOVE_KEYS[code]) return true;
    if (rotation && TDM106_TURN_KEYS[code]) return true;
  }
  return false;
}

/** Zero the rotation part unless rotation is on (translation-only driving). */
export function tdm106TranslationOnly(t: Tdm56Twist, rotation: boolean): Tdm56Twist {
  return rotation ? t : { lin: t.lin, ang: [0, 0, 0] };
}

/**
 * Smooth ramp: each axis eases toward the requested velocity, rising over TDM106_RAMP_RISE_S and
 * falling over TDM106_RAMP_FALL_S (a slew-rate limit, so a key press never jerks the arm).
 */
export class Tdm106Ramp {
  cur: Tdm56Twist = { lin: [0, 0, 0], ang: [0, 0, 0] };
  riseS: number;
  fallS: number;
  constructor(riseS = TDM106_RAMP_RISE_S, fallS = TDM106_RAMP_FALL_S) {
    this.riseS = riseS;
    this.fallS = fallS;
  }

  reset() {
    this.cur = { lin: [0, 0, 0], ang: [0, 0, 0] };
  }

  step(want: Tdm56Twist, dtS: number): Tdm56Twist {
    const ease = (c: number, w: number) => {
      // speeding up (same sign, larger magnitude) is slow; slowing down or reversing is fast
      const rising = Math.abs(w) > Math.abs(c) && (c === 0 || Math.sign(w) === Math.sign(c));
      const rate = dtS / (rising ? this.riseS : this.fallS);
      const d = w - c;
      return Math.abs(d) <= rate + 1e-9 ? w : c + Math.sign(d) * rate;
    };
    this.cur = {
      lin: this.cur.lin.map((c, i) => ease(c, want.lin[i])) as V3,
      ang: this.cur.ang.map((c, i) => ease(c, want.ang[i])) as V3,
    };
    return this.cur;
  }
}

/**
 * The gripper on one key. toggle: each G press flips open/closed. hold: G down closes, G up opens.
 * Every change starts a gripper pulse (see the header).
 */
export class Tdm106Gripper {
  value = 1; // 1 = open
  mode: Tdm106GripMode = 'toggle';
  private changedAt = -Infinity;

  set(v: number, nowMs: number) {
    const n = Math.max(0, Math.min(1, v));
    if (n !== this.value) this.changedAt = nowMs;
    this.value = n;
  }

  toggle(nowMs: number) {
    this.set(this.value > 0.5 ? 0 : 1, nowMs);
  }

  /** G pressed (not a key repeat). */
  keyDown(nowMs: number) {
    if (this.mode === 'toggle') this.toggle(nowMs);
    else this.set(0, nowMs);
  }

  /** G released. */
  keyUp(nowMs: number) {
    if (this.mode === 'hold') this.set(1, nowMs);
  }

  /** True for a short while after a change: hold the clutch (no motion) so the robot applies it. */
  pulse(nowMs: number): boolean {
    return nowMs - this.changedAt < TDM106_GRIP_PULSE_MS;
  }

  get open(): boolean {
    return this.value > 0.5;
  }
}

/**
 * Snap-to-grasp assist. Returns a sideways velocity (unit-less, like a twist's lin x/y) that pulls
 * the tool toward the object's centre, only when it is already close (within `radius` sideways and
 * `above` metres over it). Zero far away, so it never drives the arm somewhere on its own: it is
 * added to the user's input only while they are driving.
 */
export function tdm106Snap(tool: V3, obj: V3 | null, opts: { radius?: number; above?: number; gain?: number } = {}): [number, number] {
  if (!obj) return [0, 0];
  const radius = opts.radius ?? 0.05;
  const above = opts.above ?? 0.14;
  const gain = opts.gain ?? 0.8;
  const dx = obj[0] - tool[0];
  const dy = obj[1] - tool[1];
  const d = Math.hypot(dx, dy);
  const h = tool[2] - obj[2];
  if (d > radius || d < 0.002 || h > above || h < -0.03) return [0, 0];
  // proportional, saturating: full pull at >= 2 cm off, fading to 0 at the centre
  const k = (gain * Math.min(1, d / 0.02)) / d;
  return [clamp1(dx * k), clamp1(dy * k)];
}

/** "6 cm" style distance for hints. */
export function tdm106Cm(m: number): string {
  const cm = Math.abs(m) * 100;
  return cm < 1 ? `${cm.toFixed(1)} cm` : `${Math.round(cm)} cm`;
}
