// TDM-106: inverse kinematics for the practice copy of the YAM (the twin's model, yamModel.ts).
//
// Practice drives a client-side kinematic copy of the arm, so the 3D twin needs joint angles for
// "tool point at p, pointing straight down, jaws turned by yaw". This solves that with damped least
// squares on the twin's own forward kinematics (yamFk.ts), warm-started from the previous answer,
// so a few iterations per 30 Hz tick are enough.
//
// Frames: robot base frame (x forward, y left, z up), metres. The grasp site's +Z is the approach
// axis and its +Y is the jaw axis (the fingers slide along it). yaw = 0 puts the jaws along world +Y
// (how the YAM_READY pose holds them), so closing squeezes left-right as seen from behind the robot.
//
// No runtime imports on purpose (node --test loads it by URL, like yamFk.ts): pass the twin's
// kinematics in, `createTdm106Ik(createYamKinematics(YAM_MODEL), YAM_BODIES)`.

import type { Mat4, V3, YamKinematics } from '@/components/robotview/yamFk';
import type { YamBody } from '@/components/robotview/yamModel';

/** YAM_READY from yamFk.ts: the folded-out pose the solver starts from. */
const READY = [0, 0.98, 0.93, -0.73, 0, 0];
const ROT_W = 0.25; // metres per radian: position matters more than a few degrees of tilt

/** Rotation (3x3, row-major) of the tool pointing down with the jaws turned by `yaw` about +z. */
export function downRotation(yaw: number): number[] {
  const c = Math.cos(yaw);
  const s = Math.sin(yaw);
  // columns: site X, site Y (jaw axis), site Z (approach, straight down); X = Y x Z
  const X = [-c, -s, 0];
  const Y = [-s, c, 0];
  const Z = [0, 0, -1];
  return [X[0], Y[0], Z[0], X[1], Y[1], Z[1], X[2], Y[2], Z[2]];
}

/** Rz(yaw) * R: a tool rotation turned about the world vertical (how the clutch's yaw composes). */
export function yawed(R: number[], yaw: number): number[] {
  const c = Math.cos(yaw);
  const s = Math.sin(yaw);
  const Z = [c, -s, 0, s, c, 0, 0, 0, 1];
  const o = new Array(9).fill(0);
  for (let r = 0; r < 3; r++) for (let k = 0; k < 3; k++) for (let j = 0; j < 3; j++) o[r * 3 + k] += Z[r * 3 + j] * R[j * 3 + k];
  return o;
}

/** Angle of the jaw axis (tool +Y) in the world xy plane, rad, measured from world +Y. */
export function jawYaw(R: number[]): number {
  return Math.atan2(-R[1], R[4]);
}

function rotOf(m: Mat4): number[] {
  // column-major 4x4 -> row-major 3x3
  return [m[0], m[4], m[8], m[1], m[5], m[9], m[2], m[6], m[10]];
}

/** Axis-angle vector of A * B^T (row-major 3x3s): the rotation that takes B to A. */
function rotErr(A: number[], B: number[]): V3 {
  const M = new Array(9).fill(0);
  for (let r = 0; r < 3; r++) for (let c = 0; c < 3; c++) for (let k = 0; k < 3; k++) M[r * 3 + c] += A[r * 3 + k] * B[c * 3 + k];
  const tr = M[0] + M[4] + M[8];
  const th = Math.acos(Math.max(-1, Math.min(1, (tr - 1) / 2)));
  const v: V3 = [M[7] - M[5], M[2] - M[6], M[3] - M[1]];
  const s = Math.sin(th);
  if (s < 1e-6) {
    if (th < 1e-6) return [v[0] / 2, v[1] / 2, v[2] / 2];
    const ax: V3 = [Math.sqrt(Math.max(0, (M[0] + 1) / 2)), Math.sqrt(Math.max(0, (M[4] + 1) / 2)), Math.sqrt(Math.max(0, (M[8] + 1) / 2))];
    return [ax[0] * th, ax[1] * th, ax[2] * th];
  }
  const k = th / (2 * s);
  return [v[0] * k, v[1] * k, v[2] * k];
}

/** Solve A x = b for a small dense system (Gaussian elimination, partial pivoting). */
function solve(A: number[][], b: number[]): number[] {
  const n = b.length;
  const M = A.map((row, i) => [...row, b[i]]);
  for (let c = 0; c < n; c++) {
    let p = c;
    for (let r = c + 1; r < n; r++) if (Math.abs(M[r][c]) > Math.abs(M[p][c])) p = r;
    [M[c], M[p]] = [M[p], M[c]];
    const d = M[c][c] || 1e-12;
    for (let r = c + 1; r < n; r++) {
      const f = M[r][c] / d;
      for (let k = c; k <= n; k++) M[r][k] -= f * M[c][k];
    }
  }
  const x = new Array(n).fill(0);
  for (let r = n - 1; r >= 0; r--) {
    let s = M[r][n];
    for (let k = r + 1; k < n; k++) s -= M[r][k] * x[k];
    x[r] = s / (M[r][r] || 1e-12);
  }
  return x;
}

function quatOf(R: number[]): [number, number, number, number] {
  const [m00, m01, m02, m10, m11, m12, m20, m21, m22] = R;
  const tr = m00 + m11 + m22;
  if (tr > 0) {
    const s = Math.sqrt(tr + 1) * 2;
    return [s / 4, (m21 - m12) / s, (m02 - m20) / s, (m10 - m01) / s];
  }
  if (m00 > m11 && m00 > m22) {
    const s = Math.sqrt(1 + m00 - m11 - m22) * 2;
    return [(m21 - m12) / s, s / 4, (m01 + m10) / s, (m02 + m20) / s];
  }
  if (m11 > m22) {
    const s = Math.sqrt(1 + m11 - m00 - m22) * 2;
    return [(m02 - m20) / s, (m01 + m10) / s, s / 4, (m12 + m21) / s];
  }
  const s = Math.sqrt(1 + m22 - m00 - m11) * 2;
  return [(m10 - m01) / s, (m02 + m20) / s, (m12 + m21) / s, s / 4];
}

export interface IkResult {
  q: number[];
  /** Remaining tool-point error (m). */
  posErr: number;
}

export function createTdm106Ik(kin: YamKinematics, bodies: readonly YamBody[]) {
  const limits: [number, number][] = bodies
    .filter((b) => b.joint && b.joint.type === 'hinge')
    .slice(0, 6)
    .map((b) => b.joint!.range);

  function residual(q: number[], p: V3, R: number[]): number[] {
    const m = kin.graspSite(q);
    const e = rotErr(R, rotOf(m));
    return [p[0] - m[12], p[1] - m[13], p[2] - m[14], e[0] * ROT_W, e[1] * ROT_W, e[2] * ROT_W];
  }

  /**
   * Joint angles that put the tool point at `p` with tool rotation `R` (row-major 3x3, e.g.
   * `yawed(home, yaw)`). Warm-start with the previous answer (`seed`); `iters` damped
   * least-squares steps (each is 7 FK evaluations).
   */
  function ik(p: V3, R: number[], seed: readonly number[] = READY, iters = 6): IkResult {
    const q = seed.slice(0, 6);
    const h = 1e-5;
    const lambda2 = 1e-4;
    let r = residual(q, p, R);
    for (let it = 0; it < iters; it++) {
      if (Math.hypot(r[0], r[1], r[2]) < 2e-4 && Math.hypot(r[3], r[4], r[5]) < 5e-4) break;
      const J: number[][] = Array.from({ length: 6 }, () => new Array(6).fill(0));
      for (let j = 0; j < 6; j++) {
        const qh = q.slice();
        qh[j] += h;
        const rh = residual(qh, p, R);
        for (let i = 0; i < 6; i++) J[i][j] = (r[i] - rh[i]) / h; // d(pose)/dq = -d(residual)/dq
      }
      // (J^T J + lambda^2 I) dq = J^T r
      const JtJ = Array.from({ length: 6 }, (_, a) =>
        Array.from({ length: 6 }, (_, b) => J.reduce((s, row) => s + row[a] * row[b], 0) + (a === b ? lambda2 : 0)),
      );
      const Jtr = Array.from({ length: 6 }, (_, a) => J.reduce((s, row, i) => s + row[a] * r[i], 0));
      const dq = solve(JtJ, Jtr);
      const step = Math.max(...dq.map(Math.abs));
      const k = step > 0.3 ? 0.3 / step : 1; // no big jumps between ticks
      for (let j = 0; j < 6; j++) q[j] = Math.max(limits[j][0], Math.min(limits[j][1], q[j] + dq[j] * k));
      r = residual(q, p, R);
    }
    return { q, posErr: Math.hypot(r[0], r[1], r[2]) };
  }

  /** Tool point of joint angles. */
  function toolPos(q: readonly number[]): V3 {
    return kin.graspSitePos(q);
  }

  /** Tool rotation (row-major 3x3) of joint angles. */
  function toolRot(q: readonly number[]): number[] {
    return rotOf(kin.graspSite(q));
  }

  /** Tool orientation as a quaternion [w, x, y, z] (RobotLiveState.ee.quat). */
  function toolQuat(q: readonly number[]): [number, number, number, number] {
    return quatOf(rotOf(kin.graspSite(q)));
  }

  return { ik, toolPos, toolRot, toolQuat, limits, ready: READY };
}

export type Tdm106Ik = ReturnType<typeof createTdm106Ik>;
