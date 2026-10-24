// TDM-97: forward kinematics of the twin's YAM model (yamModel.ts), in plain math so it runs in node
// tests and in the ghost-trajectory path without three.js. Same convention MuJoCo uses:
//   T_body = T_parent * translate(pos) * rotate(quat) * joint(axis, q)
// Frames: robot base frame (x forward, y left, z up), metres; quaternions are [w, x, y, z].
// No imports on purpose (node --test loads it by URL): the model is passed in, see createYamKinematics.
import type { YamBody } from './yamModel';

export interface YamModelLike {
  bodies: YamBody[];
  grasp: { body: string; pos: readonly number[]; quat: readonly number[] };
  fingerTravel: number;
}

export type Mat4 = Float64Array; // column-major 4x4, like three.js
export type V3 = [number, number, number];
export type Q4 = [number, number, number, number];

export const YAM_ARM_JOINTS = ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'joint6'] as const;

/** Folded rest pose (q = 0) and the YAM_READY pose from yam-sdk-notes.md section 4. */
export const YAM_REST: number[] = [0, 0, 0, 0, 0, 0];
export const YAM_READY: number[] = [0, 0.98, 0.93, -0.73, 0, 0];

export function mat4FromPosQuat(p: readonly number[], q: readonly number[]): Mat4 {
  const [w, x, y, z] = q;
  const m = new Float64Array(16);
  m[0] = 1 - 2 * (y * y + z * z);
  m[1] = 2 * (x * y + z * w);
  m[2] = 2 * (x * z - y * w);
  m[4] = 2 * (x * y - z * w);
  m[5] = 1 - 2 * (x * x + z * z);
  m[6] = 2 * (y * z + x * w);
  m[8] = 2 * (x * z + y * w);
  m[9] = 2 * (y * z - x * w);
  m[10] = 1 - 2 * (x * x + y * y);
  m[12] = p[0];
  m[13] = p[1];
  m[14] = p[2];
  m[15] = 1;
  return m;
}

export function mul(a: Mat4, b: Mat4): Mat4 {
  const o = new Float64Array(16);
  for (let c = 0; c < 4; c++) {
    for (let r = 0; r < 4; r++) {
      let s = 0;
      for (let k = 0; k < 4; k++) s += a[k * 4 + r] * b[c * 4 + k];
      o[c * 4 + r] = s;
    }
  }
  return o;
}

export function axisAngleQuat(axis: readonly number[], angle: number): Q4 {
  const n = Math.hypot(axis[0], axis[1], axis[2]) || 1;
  const s = Math.sin(angle / 2) / n;
  return [Math.cos(angle / 2), axis[0] * s, axis[1] * s, axis[2] * s];
}

/** The joint's own motion as a transform: rotation about / translation along its axis. */
export function jointTransform(body: YamBody, value: number): Mat4 {
  const j = body.joint;
  if (!j || !value) return mat4FromPosQuat([0, 0, 0], [1, 0, 0, 0]);
  if (j.type === 'hinge') return mat4FromPosQuat([0, 0, 0], axisAngleQuat(j.axis, value));
  return mat4FromPosQuat([j.axis[0] * value, j.axis[1] * value, j.axis[2] * value], [1, 0, 0, 0]);
}

/** Gripper 0 closed .. 1 open (RobotLiveState.gripper) -> finger slide (m): 0 = closed, travel = open
 * (the MJCF's joint7/joint8; the MuJoCo sim drives ctrl = travel * gripper). */
export function gripperToSlide(g: number, travel: number): number {
  const c = Math.min(1, Math.max(0, Number.isFinite(g) ? g : 1));
  return c * travel;
}

export function createYamKinematics(model: YamModelLike) {
  /** Joint values by name: the 6 arm joints from `q`, both finger slides from the gripper opening. */
  function jointValues(q: readonly number[], gripper = 1): Record<string, number> {
    const out: Record<string, number> = {};
    YAM_ARM_JOINTS.forEach((n, i) => (out[n] = Number.isFinite(q[i]) ? q[i] : 0));
    const slide = gripperToSlide(gripper, model.fingerTravel);
    out.joint7 = slide;
    out.joint8 = slide;
    return out;
  }

  /** World (base-frame) transform of every body. */
  function bodyTransforms(q: readonly number[], gripper = 1): Record<string, Mat4> {
    const vals = jointValues(q, gripper);
    const world: Record<string, Mat4> = { world: mat4FromPosQuat([0, 0, 0], [1, 0, 0, 0]) };
    for (const b of model.bodies) {
      const local = mul(mat4FromPosQuat(b.pos, b.quat), jointTransform(b, b.joint ? (vals[b.joint.name] ?? 0) : 0));
      world[b.name] = mul(world[b.parent], local);
    }
    return world;
  }

  /** Tool point (grasp_site) transform in the base frame; +Z of its rotation is the approach axis. */
  function graspSite(q: readonly number[]): Mat4 {
    const t = bodyTransforms(q);
    return mul(t[model.grasp.body], mat4FromPosQuat(model.grasp.pos, model.grasp.quat));
  }

  function graspSitePos(q: readonly number[]): V3 {
    const m = graspSite(q);
    return [m[12], m[13], m[14]];
  }

  return { jointValues, bodyTransforms, graspSite, graspSitePos, slide: (g: number) => gripperToSlide(g, model.fingerTravel) };
}

export type YamKinematics = ReturnType<typeof createYamKinematics>;
