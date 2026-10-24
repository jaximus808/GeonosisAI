// TDM-56: input mapping for the teleop page (gamepad, keyboard, on-screen sticks) -> EE velocity,
// and the clutch integrator that turns velocity into the contract's deltas-from-clutch-down.
//
// Axes are the robot BASE frame: +x forward (away from the base), +y left, +z up.
// Rotation is axis-angle in the base frame, composed with quaternions so long rotations stay right.

export type V3 = [number, number, number];

/** Max EE speed at speed_scale = 1. The robot's TDM-45 caps still apply on top. */
export const TDM56_MAX_LIN_MPS = 0.12;
export const TDM56_MAX_ANG_RADPS = 0.9;
export const TDM56_DEADZONE = 0.12;

/** Unit-less velocity request, each axis -1..1. */
export interface Tdm56Twist {
  lin: V3;
  ang: V3;
}

export const ZERO_TWIST: Tdm56Twist = { lin: [0, 0, 0], ang: [0, 0, 0] };

export function deadzone(v: number, dz = TDM56_DEADZONE): number {
  const a = Math.abs(v);
  if (a < dz) return 0;
  return Math.sign(v) * Math.min(1, (a - dz) / (1 - dz));
}

const clamp1 = (v: number) => Math.max(-1, Math.min(1, v));

export function addTwist(a: Tdm56Twist, b: Tdm56Twist): Tdm56Twist {
  return {
    lin: [clamp1(a.lin[0] + b.lin[0]), clamp1(a.lin[1] + b.lin[1]), clamp1(a.lin[2] + b.lin[2])],
    ang: [clamp1(a.ang[0] + b.ang[0]), clamp1(a.ang[1] + b.ang[1]), clamp1(a.ang[2] + b.ang[2])],
  };
}

// ---------------------------------------------------------------- keyboard

/** Keyboard map (KeyboardEvent.code). Space is the clutch, Escape is STOP (handled by the console). */
export const TDM56_KEYS: Record<string, { axis: 'lin' | 'ang'; i: 0 | 1 | 2; s: 1 | -1; shift?: boolean }> = {
  KeyW: { axis: 'lin', i: 0, s: 1 },
  KeyS: { axis: 'lin', i: 0, s: -1 },
  KeyA: { axis: 'lin', i: 1, s: 1 },
  KeyD: { axis: 'lin', i: 1, s: -1 },
  KeyQ: { axis: 'lin', i: 2, s: 1 },
  KeyE: { axis: 'lin', i: 2, s: -1 },
  ArrowUp: { axis: 'ang', i: 1, s: -1 }, // pitch the tool down/forward
  ArrowDown: { axis: 'ang', i: 1, s: 1 },
  ArrowLeft: { axis: 'ang', i: 2, s: 1 }, // yaw left; with Shift: roll
  ArrowRight: { axis: 'ang', i: 2, s: -1 },
};

export function keyboardTwist(held: ReadonlySet<string>, shift: boolean): Tdm56Twist {
  const t: Tdm56Twist = { lin: [0, 0, 0], ang: [0, 0, 0] };
  for (const code of held) {
    const k = TDM56_KEYS[code];
    if (!k) continue;
    let i: 0 | 1 | 2 = k.i;
    if (shift && (code === 'ArrowLeft' || code === 'ArrowRight')) i = 0; // roll
    t[k.axis][i] = clamp1(t[k.axis][i] + k.s);
  }
  return t;
}

// ---------------------------------------------------------------- gamepad (standard mapping)
//
//   RT (held)        clutch            left stick      translate x / y
//   right stick Y    up / down (z)     right stick X   yaw
//   d-pad up/down    pitch             d-pad L/R       roll
//   A                toggle gripper    LB / RB         close / open gripper gradually
//   B                STOP

export interface Tdm56PadRead {
  id: string;
  clutch: boolean;
  twist: Tdm56Twist;
  /** -1 close .. +1 open, continuous */
  gripperRate: number;
  pressedA: boolean;
  pressedB: boolean;
}

const btn = (p: Gamepad, i: number) => p.buttons[i]?.pressed ?? false;
const btnV = (p: Gamepad, i: number) => p.buttons[i]?.value ?? 0;

export function readPad(p: Gamepad): Tdm56PadRead {
  const ax = (i: number) => deadzone(p.axes[i] ?? 0);
  return {
    id: p.id,
    clutch: btnV(p, 7) > 0.5,
    twist: {
      lin: [-ax(1), -ax(0), -ax(3)],
      ang: [(btn(p, 15) ? -1 : 0) + (btn(p, 14) ? 1 : 0), (btn(p, 12) ? -1 : 0) + (btn(p, 13) ? 1 : 0), -ax(2)],
    },
    gripperRate: (btn(p, 5) ? 1 : 0) - (btn(p, 4) ? 1 : 0),
    pressedA: btn(p, 0),
    pressedB: btn(p, 1),
  };
}

export function firstPad(): Gamepad | null {
  if (typeof navigator === 'undefined' || !navigator.getGamepads) return null;
  for (const p of navigator.getGamepads()) if (p && p.connected) return p;
  return null;
}

// ---------------------------------------------------------------- quaternions + clutch integrator

type Q = [number, number, number, number]; // x, y, z, w

function qMul(a: Q, b: Q): Q {
  const [ax, ay, az, aw] = a;
  const [bx, by, bz, bw] = b;
  return [
    aw * bx + ax * bw + ay * bz - az * by,
    aw * by - ax * bz + ay * bw + az * bx,
    aw * bz + ax * by - ay * bx + az * bw,
    aw * bw - ax * bx - ay * by - az * bz,
  ];
}

function qFromRotvec(v: V3): Q {
  const th = Math.hypot(v[0], v[1], v[2]);
  if (th < 1e-12) return [v[0] / 2, v[1] / 2, v[2] / 2, 1];
  const s = Math.sin(th / 2) / th;
  return [v[0] * s, v[1] * s, v[2] * s, Math.cos(th / 2)];
}

function qToRotvec(q: Q): V3 {
  let [x, y, z, w] = q;
  if (w < 0) [x, y, z, w] = [-x, -y, -z, -w];
  const s = Math.hypot(x, y, z);
  if (s < 1e-12) return [2 * x, 2 * y, 2 * z];
  const th = 2 * Math.atan2(s, w);
  return [(x / s) * th, (y / s) * th, (z / s) * th];
}

/**
 * Accumulates EE deltas from the pose at clutch-down. reset() on every clutch-down; step() each tick
 * while the clutch is held. Velocity is scaled by speed_scale here so the target never runs far
 * ahead of a speed-capped arm (the robot applies speed_scale to its own caps as well).
 */
export class Tdm56ClutchIntegrator {
  dPos: V3 = [0, 0, 0];
  private q: Q = [0, 0, 0, 1];
  /** Hard cap on how far one clutch stroke may drift from its anchor (m). */
  maxReach = 0.6;

  reset() {
    this.dPos = [0, 0, 0];
    this.q = [0, 0, 0, 1];
  }

  step(t: Tdm56Twist, dtS: number, speedScale: number) {
    const k = Math.max(0, Math.min(1, speedScale)) * dtS;
    const p: V3 = [
      this.dPos[0] + t.lin[0] * TDM56_MAX_LIN_MPS * k,
      this.dPos[1] + t.lin[1] * TDM56_MAX_LIN_MPS * k,
      this.dPos[2] + t.lin[2] * TDM56_MAX_LIN_MPS * k,
    ];
    const n = Math.hypot(...p);
    this.dPos = n > this.maxReach ? (p.map((v) => (v * this.maxReach) / n) as V3) : p;
    const w: V3 = [t.ang[0] * TDM56_MAX_ANG_RADPS * k, t.ang[1] * TDM56_MAX_ANG_RADPS * k, t.ang[2] * TDM56_MAX_ANG_RADPS * k];
    if (w[0] || w[1] || w[2]) {
      // base-frame angular velocity: left-multiply
      const q = qMul(qFromRotvec(w), this.q);
      const qn = Math.hypot(...q);
      this.q = q.map((v) => v / qn) as Q;
    }
  }

  get dRot(): V3 {
    return qToRotvec(this.q);
  }
}

export const round = (v: number, d = 5) => Math.round(v * 10 ** d) / 10 ** d;
