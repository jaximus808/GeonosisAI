// TDM-106: practice on the ticket's scene. A client-side kinematic copy of the sim robot and its
// scene spec (lib/robotview/scene.ts, TDM-104's schema): the YAM's own kinematics (the twin's
// model, solved by tdm106Ik.ts), the spec's table, objects and regions, and its success predicate
// (`inside(cube, plate)`). It renders through the same 3D twin the teleop page shows.
//
// Why a client-side copy and not the MuJoCo sim robot: practice must work while the sim robot is
// busy (baseline / eval trials, someone else's teleop) and needs no lease, recording or reset
// task. Seam for a sim-robot practice session: see PRACTICE_ON_SIM_ROBOT in PracticeConsole.tsx.
//
// It consumes exactly the TDM-56 control contract the robot gets ({clutch, d_pos, d_rot, gripper,
// speed_scale} at ~30 Hz, plus stop), with the robot's behaviour simplified:
//   - deltas apply to the pose at clutch-down; clutch:false holds the pose
//   - the gripper is applied only while the clutch is held (as teleop.py does)
//   - a 200 ms watchdog holds the arm if commands stop while the clutch is down
//   - a workspace box and the table clamp the target; an unreachable target holds (reach limit)
//   - a latched STOP freezes everything until resumed
// Physics is kinematic: a closed gripper around the object carries it; opening drops it straight
// down onto the table (or onto a fixed plate under it).
//
// No runtime imports on purpose (node --test loads it by URL). Pass the IK in.

import type { RobotLiveState, Vec3 } from '@/lib/robotview/types';
import type { SceneObject, SceneSpecV1 } from '@/lib/robotview/scene';
import type { V3 } from './tdm56Input';
import type { Tdm106Ik } from './tdm106Ik';

export interface Tdm106SimCmd {
  seq: number;
  clutch: boolean;
  d_pos: V3;
  d_rot: V3;
  gripper: number;
  speed_scale: number;
}

export type Tdm106SimEvent =
  | { k: 'grasp'; id: string }
  | { k: 'missed_grasp'; why: string }
  | { k: 'drop'; id: string; success: boolean; where: 'goal' | 'table' | 'other' }
  | { k: 'stop' }
  | { k: 'resume' }
  | { k: 'reset' };

export interface Tdm106Task {
  /** The object to move (from `inside(obj, goal)`, else the first free object). */
  obj: string;
  /** The goal: a region or a fixed object. */
  goal: string;
}

/** Parse the spec's success predicate: `inside(a, b)` / `on_top(a, b)`, or the canonical `{object, target}` form. */
export function tdm106Task(spec: SceneSpecV1): Tdm106Task | null {
  const raw = spec.success as unknown;
  if (raw && typeof raw === 'object') {
    const r = raw as { object?: unknown; target?: unknown };
    if (typeof r.object === 'string' && typeof r.target === 'string') return { obj: r.object, goal: r.target };
  }
  const m = /^\s*(?:inside|on_top)\(\s*([\w-]+)\s*,\s*([\w-]+)\s*\)\s*$/.exec(typeof raw === 'string' ? raw : '');
  if (m) return { obj: m[1], goal: m[2] };
  const obj = spec.objects.find((o) => !o.fixed);
  const goal = spec.regions?.[0]?.id ?? spec.objects.find((o) => o.fixed)?.id;
  return obj && goal ? { obj: obj.id, goal } : null;
}

const WATCHDOG_MS = 200;
const FOLLOW_TAU_S = 0.08; // the arm lags its target a little, like a servo loop
const JAW_RATE = 1.6; // opening per second (0..1)
const GAP_CLOSED = 0.005; // jaw gap at gripper 0 (m)
const GAP_OPEN = 0.09; // jaw gap at gripper 1 (m)
const REACH_TOL = 0.004; // IK error above this = unreachable, hold

const gapOf = (g: number) => GAP_CLOSED + (GAP_OPEN - GAP_CLOSED) * g;
const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));

/** Half-extents of an object (box: half sizes; cylinder: r, r, half height; sphere: r). */
function halfExtents(o: SceneObject): V3 {
  const [a = 0.02, b = a, c = a] = o.size;
  if (o.type === 'cylinder') return [a, a, b];
  if (o.type === 'sphere') return [a, a, a];
  return [a, b, c];
}

function quatYaw(q: readonly number[] | undefined): number {
  if (!q) return 0;
  const [w, x, y, z] = q;
  return Math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z));
}

const yawQuat = (yaw: number): [number, number, number, number] => [Math.cos(yaw / 2), 0, 0, Math.sin(yaw / 2)];

interface ObjState {
  spec: SceneObject;
  half: V3;
  pos: V3;
  yaw: number;
  held: boolean;
}

/** Deterministic PRNG so a "new layout" is repeatable in tests. */
function mulberry32(seed: number) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export interface Tdm106SceneSimOptions {
  spec: SceneSpecV1;
  ik: Tdm106Ik;
  /** Home joints (the sim robot's SIM_HOME = YAM_READY). */
  home?: readonly number[];
  /** Workspace box for the tool point (base frame). Default: the table, 1 cm to 32 cm up. */
  workspace?: { min: V3; max: V3 };
}

export class Tdm106SceneSim {
  readonly spec: SceneSpecV1;
  readonly task: Tdm106Task | null;
  readonly workspace: { min: V3; max: V3 };
  q: number[];
  /** Tool point (actual) and its target. */
  tool: V3;
  yaw = 0; // tool yaw about +z relative to home (rad)
  gripperCmd = 1; // commanded 0 closed .. 1 open
  jaw = 1; // actual opening 0..1 (stops on the object)
  clutch = false;
  stopped = false;
  watchdog = false;
  clamps: string[] = [];
  succeeded = false;
  objects = new Map<string, ObjState>();
  private ik: Tdm106Ik;
  private home: number[];
  private homeRot: number[];
  private homeTool: V3;
  private target: { p: V3; yaw: number };
  private anchor: { p: V3; yaw: number };
  private lastCmdAt = 0;
  private holdOffset: V3 = [0, 0, 0];
  private events: Tdm106SimEvent[] = [];
  private clampCounts: Record<string, number[]> = {};

  constructor(opts: Tdm106SceneSimOptions) {
    this.spec = opts.spec;
    this.ik = opts.ik;
    this.task = tdm106Task(opts.spec);
    this.home = [...(opts.home ?? opts.ik.ready)];
    this.homeRot = opts.ik.toolRot(this.home);
    this.homeTool = opts.ik.toolPos(this.home);
    const t = opts.spec.table;
    this.workspace = opts.workspace ?? {
      min: [Math.max(0.12, t.center[0] - t.size[0] / 2 + 0.02), Math.max(-0.34, t.center[1] - t.size[1] / 2 + 0.02), t.top_z + 0.008],
      max: [Math.min(0.52, t.center[0] + t.size[0] / 2 - 0.02), Math.min(0.34, t.center[1] + t.size[1] / 2 - 0.02), t.top_z + 0.32],
    };
    this.q = [...this.home];
    this.tool = [...this.homeTool];
    this.target = { p: [...this.homeTool], yaw: 0 };
    this.anchor = { p: [...this.homeTool], yaw: 0 };
    this.placeObjects(null);
  }

  private placeObjects(rand: (() => number) | null) {
    this.objects.clear();
    for (const o of this.spec.objects) {
      const pos = [...o.pose.pos] as V3;
      let yaw = quatYaw(o.pose.quat);
      if (rand && !o.fixed && o.randomize) {
        const [lo, hi] = o.randomize.pos ?? [[0, 0, 0], [0, 0, 0]];
        for (let i = 0; i < 2; i++) pos[i] += lo[i] + (hi[i] - lo[i]) * rand();
        if (o.randomize.yaw) yaw += o.randomize.yaw[0] + (o.randomize.yaw[1] - o.randomize.yaw[0]) * rand();
      }
      this.objects.set(o.id, { spec: o, half: halfExtents(o), pos, yaw, held: false });
    }
  }

  /** Back to the start: arm home, gripper open, objects at their spec poses (or a new random layout). */
  reset(seed?: number) {
    this.placeObjects(seed == null ? null : mulberry32(seed));
    this.q = [...this.home];
    this.tool = [...this.homeTool];
    this.target = { p: [...this.homeTool], yaw: 0 };
    this.anchor = { p: [...this.homeTool], yaw: 0 };
    this.yaw = 0;
    this.gripperCmd = 1;
    this.jaw = 1;
    this.clutch = false;
    this.watchdog = false;
    this.succeeded = false;
    this.clamps = [];
    this.events.push({ k: 'reset' });
  }

  stop() {
    if (!this.stopped) this.events.push({ k: 'stop' });
    this.stopped = true;
    this.clutch = false;
    this.target = { p: [...this.tool], yaw: this.yaw };
  }

  resume() {
    if (this.stopped) this.events.push({ k: 'resume' });
    this.stopped = false;
  }

  drainEvents(): Tdm106SimEvent[] {
    const e = this.events;
    this.events = [];
    return e;
  }

  private heldObj(): ObjState | null {
    for (const o of this.objects.values()) if (o.held) return o;
    return null;
  }

  /** Height a dropped object rests at, at (x, y): the table, or the top of whatever is under it (a plate, a block). */
  private restZ(o: ObjState, x: number, y: number): number {
    let base = this.spec.table.top_z;
    for (const f of this.objects.values()) {
      if (f === o || f.held) continue;
      const r = f.spec.type === 'cylinder' ? f.half[0] : Math.max(f.half[0], f.half[1]);
      if (Math.hypot(x - f.pos[0], y - f.pos[1]) <= r) base = Math.max(base, f.pos[2] + f.half[2]);
    }
    return base + o.half[2];
  }

  /** The goal's centre and radius (region disc / box, or a fixed object's footprint). */
  goal(): { pos: V3; radius: number } | null {
    if (!this.task) return null;
    const r = this.spec.regions?.find((g) => g.id === this.task!.goal);
    if (r) return { pos: [r.pos[0], r.pos[1], this.spec.table.top_z], radius: r.size[0] };
    const f = this.objects.get(this.task.goal);
    return f ? { pos: [f.pos[0], f.pos[1], f.pos[2] + f.half[2]], radius: f.half[0] } : null;
  }

  /** inside(obj, goal): the object's centre is within the goal's footprint and it rests (not held). */
  isSuccess(): boolean {
    const o = this.task ? this.objects.get(this.task.obj) : null;
    const g = this.goal();
    if (!o || !g || o.held) return false;
    return Math.hypot(o.pos[0] - g.pos[0], o.pos[1] - g.pos[1]) <= g.radius - 0.005;
  }

  /** The object to pick, for the snap assist and the coach. */
  taskObject(): { id: string; pos: V3; half: V3; held: boolean } | null {
    const o = this.task ? this.objects.get(this.task.obj) : null;
    return o ? { id: o.spec.id, pos: [...o.pos] as V3, half: o.half, held: o.held } : null;
  }

  /** One contract command, applied when it "arrives" (after any simulated network delay). */
  command(c: Tdm106SimCmd, nowMs: number) {
    if (this.stopped) return;
    this.lastCmdAt = nowMs;
    this.watchdog = false;
    if (c.clutch && !this.clutch) this.anchor = { p: [...this.tool], yaw: this.yaw };
    this.clutch = c.clutch;
    if (!c.clutch) {
      this.target = { p: [...this.tool], yaw: this.yaw };
      return;
    }
    const want: V3 = [this.anchor.p[0] + c.d_pos[0], this.anchor.p[1] + c.d_pos[1], this.anchor.p[2] + c.d_pos[2]];
    const { min, max } = this.workspace;
    const lim: V3 = [clamp(want[0], min[0], max[0]), clamp(want[1], min[1], max[1]), clamp(want[2], min[2], max[2])];
    const clamps: string[] = [];
    if (lim[0] !== want[0] || lim[1] !== want[1] || want[2] > max[2]) clamps.push('clamped_workspace');
    // the held object (or the fingers) cannot go through the table
    const h = this.heldObj();
    const floor = h ? this.restZ(h, lim[0], lim[1]) - this.holdOffset[2] + 0.001 : min[2];
    if (lim[2] < floor) lim[2] = floor;
    if (want[2] < floor - 1e-4) clamps.push('table');
    this.setClamps(clamps, nowMs);
    this.target = { p: lim, yaw: clamp(this.anchor.yaw + c.d_rot[2], -Math.PI / 2, Math.PI / 2) };
    this.gripperCmd = clamp(c.gripper, 0, 1);
  }

  private setClamps(c: string[], nowMs: number) {
    this.clamps = c;
    for (const k of c) (this.clampCounts[k] ??= []).push(nowMs);
  }

  /** Advance by dt seconds. */
  step(dtS: number, nowMs: number) {
    for (const k of Object.keys(this.clampCounts)) this.clampCounts[k] = this.clampCounts[k].filter((t) => nowMs - t < 1000);
    if (this.stopped) return;
    if (this.clutch && nowMs - this.lastCmdAt > WATCHDOG_MS) {
      this.watchdog = true;
      this.target = { p: [...this.tool], yaw: this.yaw };
    }
    // the tool follows its target; the IK says whether the arm can get there
    const a = 1 - Math.exp(-dtS / FOLLOW_TAU_S);
    const next: V3 = [0, 1, 2].map((i) => this.tool[i] + (this.target.p[i] - this.tool[i]) * a) as V3;
    const nextYaw = this.yaw + (this.target.yaw - this.yaw) * a;
    const sol = this.ik.ik(next, yawRot(this.homeRot, nextYaw), this.q, 4);
    if (sol.posErr <= REACH_TOL) {
      this.q = sol.q;
      this.tool = next;
      this.yaw = nextYaw;
    } else {
      // unreachable: hold where we are, like the robot's IK / workspace clamp
      this.target = { p: [...this.tool], yaw: this.yaw };
      this.setClamps([...this.clamps.filter((c) => c !== 'reach'), 'reach'], nowMs);
    }

    // jaws (the robot applies the gripper only while the clutch is held: c.gripper arrives only then)
    const prev = this.jaw;
    this.jaw += clamp(this.gripperCmd - this.jaw, -JAW_RATE * dtS, JAW_RATE * dtS);
    const held = this.heldObj();
    const o = this.task ? this.objects.get(this.task.obj) : null;
    if (!held && o) {
      const width = 2 * Math.min(o.half[0], o.half[1]);
      if (gapOf(prev) > width && gapOf(this.jaw) <= width) {
        const dxy = Math.hypot(this.tool[0] - o.pos[0], this.tool[1] - o.pos[1]);
        const dz = this.tool[2] - o.pos[2];
        if (dxy < 0.022 && Math.abs(dz) < Math.max(0.02, o.half[2] + 0.006)) {
          o.held = true;
          // closing squares the block up in the jaws
          const jaw = this.yaw;
          const q = Math.PI / 2;
          o.yaw = jaw + Math.round((o.yaw - jaw) / q) * q;
          this.holdOffset = [o.pos[0] - this.tool[0], o.pos[1] - this.tool[1], o.pos[2] - this.tool[2]];
          this.events.push({ k: 'grasp', id: o.spec.id });
        } else if (dxy < 0.08) {
          const why =
            dxy >= 0.022
              ? 'Missed: the fingers were not around it. Line the hand up over its centre first.'
              : dz > 0
                ? 'Missed: too high. Lower the hand around it (E) before closing.'
                : 'Missed: too low. Lift a little (Q) and try again.';
          this.events.push({ k: 'missed_grasp', why });
        }
      }
    }
    const h = this.heldObj();
    if (h) {
      const width = 2 * Math.min(h.half[0], h.half[1]);
      if (gapOf(this.jaw) < width) this.jaw = (width - GAP_CLOSED) / (GAP_OPEN - GAP_CLOSED);
      h.pos = [this.tool[0] + this.holdOffset[0], this.tool[1] + this.holdOffset[1], this.tool[2] + this.holdOffset[2]];
      if (gapOf(this.gripperCmd) > width + 0.01 && gapOf(this.jaw) > width + 0.004) {
        h.held = false;
        h.pos = [h.pos[0], h.pos[1], this.restZ(h, h.pos[0], h.pos[1])];
        const success = this.isSuccess();
        const g = this.goal();
        const nearGoal = g ? Math.hypot(h.pos[0] - g.pos[0], h.pos[1] - g.pos[1]) <= g.radius + 0.03 : false;
        this.succeeded = success;
        this.events.push({ k: 'drop', id: h.spec.id, success, where: success ? 'goal' : nearGoal ? 'other' : 'table' });
      }
    }
  }

  /**
   * The "landing ghost": the arm with the tool lowered straight down to where it would touch
   * (grasp height over the object, or where the held object would rest). Shows, before you lower,
   * whether the fingers will straddle the object or the object will land on the goal. Null when the
   * hand is already there.
   */
  landingJoints(): number[] | null {
    const h = this.heldObj();
    let z: number;
    if (h) z = this.restZ(h, h.pos[0], h.pos[1]) - this.holdOffset[2];
    else {
      const o = this.task ? this.objects.get(this.task.obj) : null;
      z = o ? o.pos[2] : this.workspace.min[2];
      if (o && Math.hypot(this.tool[0] - o.pos[0], this.tool[1] - o.pos[1]) > 0.06) z = Math.max(this.workspace.min[2], this.spec.table.top_z + 0.02);
    }
    if (this.tool[2] - z < 0.01) return null;
    const sol = this.ik.ik([this.tool[0], this.tool[1], z], yawRot(this.homeRot, this.yaw), this.q, 10);
    return sol.posErr <= REACH_TOL * 2 ? sol.q : null;
  }

  /** Joints for the twin's ghost: where the arm is heading (IK of the target), or null if there. */
  targetJoints(): number[] | null {
    const d = Math.hypot(this.target.p[0] - this.tool[0], this.target.p[1] - this.tool[1], this.target.p[2] - this.tool[2]);
    if (d < 0.003) return null;
    const sol = this.ik.ik(this.target.p, yawRot(this.homeRot, this.target.yaw), this.q, 6);
    return sol.posErr <= REACH_TOL * 2 ? sol.q : null;
  }

  /** The E15 RobotLiveState for the 3D twin (labelled practice; nothing here comes from a robot). */
  liveState(nowMs: number): RobotLiveState {
    const objects: Record<string, { pos: Vec3; quat: [number, number, number, number] }> = {};
    for (const [id, o] of this.objects) objects[id] = { pos: [...o.pos] as Vec3, quat: yawQuat(o.yaw) };
    const clamps_1s: Record<string, number> = {};
    for (const [k, v] of Object.entries(this.clampCounts)) if (v.length) clamps_1s[k] = v.length;
    const cube = this.task ? objects[this.task.obj] : undefined;
    const g = this.goal();
    return {
      t_ms: nowMs,
      joints: [...this.q],
      gripper: this.jaw,
      ee: { pos: [...this.tool] as Vec3, quat: this.ik.toolQuat(this.q) },
      control_source: this.clutch ? 'human' : 'idle',
      robot_state: this.stopped ? 'estop' : 'practice',
      estop: this.stopped,
      safety: { clamps_1s },
      scene: { objects, ...(cube ? { cube } : {}), ...(g ? { plate: { pos: g.pos as Vec3 } } : {}) } as RobotLiveState['scene'],
      workspace: { min: this.workspace.min as Vec3, max: this.workspace.max as Vec3, table_z: this.spec.table.top_z },
    };
  }
}

function yawRot(R: number[], yaw: number): number[] {
  const c = Math.cos(yaw);
  const s = Math.sin(yaw);
  const Z = [c, -s, 0, s, c, 0, 0, 0, 1];
  const o = new Array(9).fill(0);
  for (let r = 0; r < 3; r++) for (let k = 0; k < 3; k++) for (let j = 0; j < 3; j++) o[r * 3 + k] += Z[r * 3 + j] * R[j * 3 + k];
  return o;
}
