// The landing hero's pick-and-place loop: joint keyframes for the real YAM model (yamModel.ts), solved
// offline with its forward kinematics (yamFk.ts) so the grasp site lands on the sim scene's cube
// (0.31, -0.14) and plate (0.31, 0.19) with the approach axis straight down. Pure, no three.js.
// Robot base frame: x forward, y left, z up, metres.

export type Q6 = [number, number, number, number, number, number];

export const HERO_LOOP_S = 16;

/** Gripper command (RobotLiveState.gripper convention): open and closed on the 4 cm cube. */
export const GRIP_OPEN = 0.15;
export const GRIP_CLOSED = 0.62;

export const HERO_POSES: Record<'home' | 'aboveCube' | 'atCube' | 'lift' | 'abovePlate' | 'atPlate', Q6> = {
  home: [0, 1.647, 1.719, -1.643, 0, 1.57],
  aboveCube: [-0.424, 1.833, 1.655, -1.393, 0, 1.147],
  atCube: [-0.424, 1.893, 1.298, -0.976, 0, 1.147],
  lift: [-0.424, 1.833, 1.655, -1.393, 0, 1.147],
  abovePlate: [0.549, 1.933, 1.828, -1.465, 0, 0.524],
  atPlate: [0.549, 1.963, 1.446, -1.053, 0, 0.524],
};

/** Cube centre (4 cm, yaw 0) at the pick spot; plate centre (r 7.5 cm, 1.2 cm tall) on the floor. */
export const CUBE_HALF = 0.02;
export const CUBE_START: [number, number, number] = [0.31, -0.14, CUBE_HALF];
export const PLATE_POS: [number, number, number] = [0.31, 0.19, 0.006];

interface Key { t: number; q: Q6; g: number }
const P = HERO_POSES;
const KEYS: Key[] = [
  { t: 0, q: P.home, g: GRIP_OPEN },
  { t: 2.0, q: P.aboveCube, g: GRIP_OPEN },
  { t: 3.2, q: P.atCube, g: GRIP_OPEN },
  { t: 4.0, q: P.atCube, g: GRIP_CLOSED },
  { t: 5.0, q: P.lift, g: GRIP_CLOSED },
  { t: 7.4, q: P.abovePlate, g: GRIP_CLOSED },
  { t: 8.6, q: P.atPlate, g: GRIP_CLOSED },
  { t: 9.3, q: P.atPlate, g: GRIP_OPEN },
  { t: 10.3, q: P.abovePlate, g: GRIP_OPEN },
  { t: 12.4, q: P.home, g: GRIP_OPEN },
  { t: HERO_LOOP_S, q: P.home, g: GRIP_OPEN },
];

/** The cube rides the gripper between these times (fully closed .. starts opening). */
export const T_GRAB = 4.0;
export const T_RELEASE = 8.6;
/** The placed cube melts away, and a new one is poured at the pick spot and cools from melt to red. */
export const T_DISSOLVE = [12.8, 13.6] as const;
export const T_POUR = [13.6, 14.6] as const;
export const T_COOL_END = 15.8;

export const HERO_STAGES = ['baseline 1/3', 'diagnose', '+1 demo', 'eval 3/3', 'shipped'] as const;
const STAGE_START = [0, 3.2, 5.0, 8.6, 12.4];

const smoother = (u: number) => u * u * u * (u * (u * 6 - 15) + 10);
const clamp01 = (u: number) => Math.min(1, Math.max(0, u));
export const ramp = (t: number, a: number, b: number) => smoother(clamp01((t - a) / (b - a)));

export function heroArm(t: number): { q: Q6; gripper: number } {
  const tt = ((t % HERO_LOOP_S) + HERO_LOOP_S) % HERO_LOOP_S;
  let i = 0;
  while (i < KEYS.length - 2 && tt >= KEYS[i + 1].t) i++;
  const a = KEYS[i];
  const b = KEYS[i + 1];
  const u = smoother(clamp01((tt - a.t) / (b.t - a.t)));
  const q = a.q.map((v, k) => v + (b.q[k] - v) * u) as Q6;
  return { q, gripper: a.g + (b.g - a.g) * u };
}

export function heroStage(t: number): number {
  const tt = ((t % HERO_LOOP_S) + HERO_LOOP_S) % HERO_LOOP_S;
  let s = 0;
  for (let i = 0; i < STAGE_START.length; i++) if (tt >= STAGE_START[i]) s = i;
  return s;
}

/** The frozen frame drawn under prefers-reduced-motion: the cube held over the plate. */
export const HERO_STILL_T = 8.0;
