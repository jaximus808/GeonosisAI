// TDM-59: client-side simulated arm for practice mode.
//
// It consumes exactly the TDM-56 control contract the real robot gets ({t:"cmd", clutch, d_pos,
// d_rot, gripper, speed_scale} at ~30 Hz, and {t:"stop"}), so practising here is practising the
// real mapping. Nothing leaves the browser: no robot, no lease, no recording.
//
// World: the robot BASE frame of the contract (+x forward, +y left, +z up, metres) on a table at z=0.
// Behaviour copied from the real stack, simplified:
//   - deltas are applied to the pose at clutch-down; clutch:false holds the pose
//   - a 200 ms watchdog holds the arm if commands stop while the clutch is down (TDM-45)
//   - a workspace box clamps the target and reports `clamped_workspace`
//   - a latched STOP freezes everything until resumed
// Scene: one block to pick up and one bin to drop it in.

import type { V3 } from '@/lib/teleop/tdm56Input';

export interface SimCmd {
  seq: number;
  clutch: boolean;
  d_pos: V3;
  d_rot: V3;
  gripper: number;
  speed_scale: number;
}

export const SIM_WS = { x: [0.14, 0.56], y: [-0.32, 0.32], z: [0.012, 0.4] } as const;
export const SIM_BLOCK = 0.04; // cube edge (m)
export const SIM_BIN = { x: [0.16, 0.28], y: [-0.3, -0.16], h: 0.06 } as const; // near right: clear of the phone inset
export const SIM_SHOULDER: V3 = [0, 0, 0.13];
export const SIM_LINKS = [0.3, 0.3] as const;
const START: V3 = [0.3, 0.06, 0.2];
const BLOCK_START = { pos: [0.44, 0.16, SIM_BLOCK / 2] as V3, yaw: (28 * Math.PI) / 180 };
const WATCHDOG_MS = 200;
const FOLLOW_TAU_S = 0.08; // the arm lags its target a little, like a real servo loop
const MAX_JAW = 0.085; // fully open jaw gap (m)

export interface SimBlock {
  pos: V3;
  yaw: number;
  held: boolean;
  inBin: boolean;
}

export interface SimState {
  t: number;
  ee: V3; // gripper tip
  yaw: number; // rotation about +z (rad); the only rotation that matters for this scene
  tilt: number; // |rotation about x/y| (rad), shown so tilting is visible
  gripper: number; // 0 closed .. 1 open (commanded)
  jaw: number; // actual jaw gap (m), stops on the block
  block: SimBlock;
  clutch: boolean;
  stopped: boolean;
  watchdog: boolean;
  clamps: string[];
  /** Events since the last read (for the lesson tracker). */
  events: SimEvent[];
}

export type SimEvent =
  | { k: 'grasp' }
  | { k: 'drop'; inBin: boolean }
  | { k: 'missed_grasp'; why: string }
  | { k: 'stop' }
  | { k: 'resume' }
  | { k: 'reset' };

function rotvecZ(r: V3): number {
  // yaw part of a small-ish axis-angle rotation
  return r[2];
}

const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));

/** Smallest angle between the jaw axis and a block face (block is square: 90° symmetric). */
export function yawMisalign(a: number, b: number): number {
  const q = Math.PI / 2;
  let d = (a - b) % q;
  if (d < 0) d += q;
  return Math.min(d, q - d);
}

export class Tdm59SimArm {
  s: SimState;
  private anchor = { ee: START as V3, yaw: 0, tiltVec: [0, 0] as [number, number] };
  private target = { ee: [...START] as V3, yaw: 0, tilt: 0 };
  private lastCmdAt = 0;

  constructor() {
    this.s = Tdm59SimArm.initial();
  }

  static initial(): SimState {
    return {
      t: 0,
      ee: [...START] as V3,
      yaw: 0,
      tilt: 0,
      gripper: 1,
      jaw: MAX_JAW,
      block: { pos: [...BLOCK_START.pos] as V3, yaw: BLOCK_START.yaw, held: false, inBin: false },
      clutch: false,
      stopped: false,
      watchdog: false,
      clamps: [],
      events: [],
    };
  }

  reset() {
    const ev = this.s.events;
    this.s = Tdm59SimArm.initial();
    this.s.events = [...ev, { k: 'reset' }];
    this.target = { ee: [...START] as V3, yaw: 0, tilt: 0 };
  }

  stop() {
    if (!this.s.stopped) this.s.events.push({ k: 'stop' });
    this.s.stopped = true;
    this.s.clutch = false;
    this.target = { ee: [...this.s.ee] as V3, yaw: this.s.yaw, tilt: this.s.tilt };
  }

  resume() {
    if (this.s.stopped) this.s.events.push({ k: 'resume' });
    this.s.stopped = false;
  }

  /** One contract command, applied when it "arrives" (after any simulated network delay). */
  command(c: SimCmd, nowMs: number) {
    if (this.s.stopped) return;
    this.lastCmdAt = nowMs;
    this.s.watchdog = false;
    if (c.clutch && !this.s.clutch) {
      // clutch-down: deltas are measured from here
      this.anchor = { ee: [...this.s.ee] as V3, yaw: this.s.yaw, tiltVec: [0, 0] };
    }
    this.s.clutch = c.clutch;
    if (!c.clutch) {
      this.target = { ee: [...this.s.ee] as V3, yaw: this.s.yaw, tilt: this.s.tilt };
      return;
    }
    const clamps: string[] = [];
    const want: V3 = [this.anchor.ee[0] + c.d_pos[0], this.anchor.ee[1] + c.d_pos[1], this.anchor.ee[2] + c.d_pos[2]];
    const lim: V3 = [clamp(want[0], SIM_WS.x[0], SIM_WS.x[1]), clamp(want[1], SIM_WS.y[0], SIM_WS.y[1]), clamp(want[2], SIM_WS.z[0], SIM_WS.z[1])];
    // the held block (or the bare fingers) cannot go through the table
    const floor = this.s.block.held ? SIM_BLOCK / 2 + 0.002 : SIM_WS.z[0];
    lim[2] = Math.max(lim[2], floor);
    if (lim.some((v, i) => Math.abs(v - want[i]) > 1e-4)) clamps.push(want[2] < floor + 1e-4 && Math.abs(lim[2] - want[2]) > 1e-4 ? 'table' : 'clamped_workspace');
    this.s.clamps = clamps;
    this.target = {
      ee: lim,
      yaw: this.anchor.yaw + rotvecZ(c.d_rot),
      tilt: Math.hypot(c.d_rot[0], c.d_rot[1]),
    };
    this.s.gripper = clamp(c.gripper, 0, 1);
  }

  /** Advance physics by dt seconds. */
  step(dtS: number, nowMs: number) {
    const s = this.s;
    s.t += dtS;
    if (s.stopped) return;
    if (s.clutch && nowMs - this.lastCmdAt > WATCHDOG_MS) {
      s.watchdog = true;
      this.target = { ee: [...s.ee] as V3, yaw: s.yaw, tilt: s.tilt };
    }
    const a = 1 - Math.exp(-dtS / FOLLOW_TAU_S);
    for (let i = 0; i < 3; i++) s.ee[i] += (this.target.ee[i] - s.ee[i]) * a;
    s.yaw += (this.target.yaw - s.yaw) * a;
    s.tilt += (this.target.tilt - s.tilt) * a;

    // jaws: move toward the commanded gap; closing past the block's width either grips it
    // (hand around the block, fingers lined up with its faces) or closes on nothing
    const wantJaw = s.gripper * MAX_JAW;
    const jawRate = 0.12 * dtS;
    const prevJaw = s.jaw;
    s.jaw += clamp(wantJaw - s.jaw, -jawRate, jawRate);
    const b = s.block;

    if (!b.held && prevJaw > SIM_BLOCK && s.jaw <= SIM_BLOCK) {
      const dxy = Math.hypot(s.ee[0] - b.pos[0], s.ee[1] - b.pos[1]);
      const dz = s.ee[2] - b.pos[2];
      const mis = yawMisalign(s.yaw, b.yaw);
      if (dxy < 0.028 && Math.abs(dz) < 0.03 && mis < (22 * Math.PI) / 180) {
        b.held = true;
        b.inBin = false;
        s.jaw = SIM_BLOCK;
        s.events.push({ k: 'grasp' });
      } else if (dxy < 0.08) {
        const why =
          dxy >= 0.028
            ? 'Missed: the fingers were not around the block. Center the hand over it.'
            : dz >= 0.03
              ? 'Missed: too high. Lower the hand around the block before closing.'
              : 'Missed: turn the hand so the fingers line up with the block faces.';
        s.events.push({ k: 'missed_grasp', why });
      }
    }
    if (b.held) {
      s.jaw = Math.max(s.jaw, SIM_BLOCK);
      b.pos = [s.ee[0], s.ee[1], Math.max(SIM_BLOCK / 2, s.ee[2])];
      b.yaw = s.yaw;
      if (s.gripper > 0.55) {
        b.held = false;
        b.inBin = b.pos[0] > SIM_BIN.x[0] && b.pos[0] < SIM_BIN.x[1] && b.pos[1] > SIM_BIN.y[0] && b.pos[1] < SIM_BIN.y[1];
        b.pos = [b.pos[0], b.pos[1], SIM_BLOCK / 2];
        s.events.push({ k: 'drop', inBin: b.inBin });
      }
    }
  }

  drainEvents(): SimEvent[] {
    const e = this.s.events;
    this.s.events = [];
    return e;
  }
}

/** Planar 2-link elbow for drawing: returns the elbow point for shoulder S and wrist W. */
export function elbowFor(S: V3, W: V3): V3 {
  const [l1, l2] = SIM_LINKS;
  const r = Math.hypot(W[0] - S[0], W[1] - S[1]);
  const dz = W[2] - S[2];
  const d = clamp(Math.hypot(r, dz), 1e-6, l1 + l2 - 1e-6);
  const a = Math.atan2(dz, r) + Math.acos(clamp((l1 * l1 + d * d - l2 * l2) / (2 * l1 * d), -1, 1));
  const az = Math.atan2(W[1] - S[1], W[0] - S[0]);
  const er = l1 * Math.cos(a);
  return [S[0] + er * Math.cos(az), S[1] + er * Math.sin(az), S[2] + l1 * Math.sin(a)];
}
