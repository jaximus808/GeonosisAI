// Teleop command safety (pure; apps/web/test/teleop-safety.test.ts).
//
// 1. The TDM-106 snap-to-grasp assist nudges the hand toward an object position that only a SIM scene knows.
//    It is allowed only on a sim robot (camera_config_json.sim === true, the TDM-107 rule). On a real robot the
//    console never takes an assist target and the toggle is hidden.
// 2. What goes on the /control wire is the TDM-56 contract and nothing else: deltas from the pose at clutch-down
//    (d_pos m, d_rot rad), the gripper (0..1) and the speed scale, plus the TDM-57 lease stamp. No client-side IK
//    target, joint vector or twin pose can ride along, whatever the drive loop or a slot adds to the object.

type RobotLike = { cameraConfigJson?: string | null } | undefined | null;

/** Snap-to-grasp may run only on a sim robot: a literal `"sim": true` in its camera config (the same rule as
 *  lib/tdm107/robotMode.ts tdm107IsSimRobot; kept import-free so it runs under node --test). No robot row yet,
 *  bad JSON, `"sim": "true"`, anything else: not allowed. */
export function teleopAssistAllowed(robot: RobotLike): boolean {
  if (!robot || !robot.cameraConfigJson) return false;
  try {
    const cfg = JSON.parse(robot.cameraConfigJson) as unknown;
    return !!cfg && typeof cfg === 'object' && !Array.isArray(cfg) && (cfg as Record<string, unknown>).sim === true;
  } catch {
    return false;
  }
}

/** The fields a motion command may carry on the wire (TDM-56 contract + TDM-57 stamp). */
export const TDM56_CMD_FIELDS = ['seq', 'clutch', 'd_pos', 'd_rot', 'gripper', 'speed_scale', 'task_id', 'claimed_at_us'] as const;

export interface Tdm56WireCmd {
  seq: number;
  clutch: boolean;
  d_pos: [number, number, number];
  d_rot: [number, number, number];
  gripper: number;
  speed_scale: number;
  task_id?: number;
  claimed_at_us?: number;
}

const v3 = (v: unknown): [number, number, number] | null =>
  Array.isArray(v) && v.length === 3 && v.every((x) => typeof x === 'number' && Number.isFinite(x)) ? [v[0], v[1], v[2]] : null;
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);

/**
 * Rebuild a motion command from its contract fields only. Anything else on the object is dropped. Returns null
 * for a malformed command (non-finite numbers, wrong shapes); then nothing is sent.
 */
export function tdm56WireCmd(cmd: object): Tdm56WireCmd | null {
  const c = cmd as Record<string, unknown>;
  const seq = num(c.seq);
  const d_pos = v3(c.d_pos);
  const d_rot = v3(c.d_rot);
  const gripper = num(c.gripper);
  const speed = num(c.speed_scale);
  if (seq == null || typeof c.clutch !== 'boolean' || !d_pos || !d_rot || gripper == null || speed == null) return null;
  const out: Tdm56WireCmd = { seq, clutch: c.clutch, d_pos, d_rot, gripper: Math.min(1, Math.max(0, gripper)), speed_scale: Math.min(1, Math.max(0, speed)) };
  const tid = num(c.task_id);
  const at = num(c.claimed_at_us);
  if (tid != null) out.task_id = tid;
  if (at != null) out.claimed_at_us = at;
  return out;
}
