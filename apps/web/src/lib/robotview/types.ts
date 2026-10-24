/**
 * E15 Robot view contract (TDM-96 owns this file; TDM-97/98/99 import it).
 * Spec: docs/WORKER_GUIDE.md, "E15 Robot view contract".
 *
 * Wire format: the robot client's TDM-55 stream server publishes one JSON
 * message per tick on `/ws/state`:  { t: "state", v: 1, ...RobotLiveState }.
 */

export type Vec3 = [number, number, number];
/** Quaternion, scalar first: [w, x, y, z]. */
export type Quat = [number, number, number, number];

export type ControlSource = 'policy' | 'human' | 'idle';

export interface RobotLiveState {
  /** Robot-side wall clock, ms since the unix epoch, when the state was sampled. */
  t_ms: number;
  /** Joint positions, rad, 6 arm joints (gripper excluded). */
  joints: number[];
  /** Gripper opening, 0 closed .. 1 open. */
  gripper: number;
  /** End-effector pose in the robot base frame. */
  ee: { pos: Vec3 /* m */; quat: Quat };
  control_source: ControlSource;
  /** Robot state machine (e.g. idle | rollout | teleop | estop | onboarding | ...). */
  robot_state: string;
  estop: boolean;
  /** Safety-layer clamp counts over the last second, keyed by clamp reason. */
  safety: { clamps_1s: Record<string, number> };
  checkpoint_uri?: string;
  /** Sim only: scene object poses (absent on the live robot). */
  scene?: { cube?: { pos: Vec3; quat: Quat }; plate?: { pos: Vec3 } };
  /** Downsampled joint targets of the current policy action chunk. */
  chunk_preview?: number[][];
  workspace?: { min: Vec3; max: Vec3; table_z: number };
}

export type RobotLiveSource = 'sim' | 'live';

/** Return value of `useRobotLive(robotId)` (apps/web/src/lib/robotview/useRobotLive.ts). */
export interface RobotLive {
  state: RobotLiveState | null;
  /** Measured message rate over the last ~1 s. */
  hz: number;
  /** now - state.t_ms (clock-offset corrected when possible), ms. */
  latencyMs: number | null;
  source: RobotLiveSource;
  /** True when no fresh state arrived in the last 1 s (or the feed is down). */
  stale: boolean;
}

/** One message on `/ws/state`. */
export type RobotStateMessage = { t: 'state'; v: 1; sim: boolean } & RobotLiveState;
