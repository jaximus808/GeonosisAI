// TDM-107: per-ticket robot mode. A ticket runs on the SIM robot (the MuJoCo YAM in its deployed 3D
// scene) or on the REAL YAM. There is no mode column: a robot is sim when its camera_config_json has
// "sim": true (the robot client sets it for the sim adapters), and a ticket's mode is its robot's.
// Every task / eval_run / episode row keeps the robot_id it ran on, so each row is labelled by its
// own robot, however often the ticket switches (services/orchestrator/.../robot_mode.py).

export type Tdm107Mode = 'sim' | 'real';

type RobotLike = { cameraConfigJson?: string | null } | undefined | null;

/** Same rule as the orchestrator and the set_ticket_robot reducer: only a literal `"sim": true`. */
export function tdm107IsSimRobot(r: RobotLike): boolean {
  if (!r || !r.cameraConfigJson) return false;
  try {
    const cfg = JSON.parse(r.cameraConfigJson) as unknown;
    return !!cfg && typeof cfg === 'object' && (cfg as Record<string, unknown>).sim === true;
  } catch {
    return false;
  }
}

export function tdm107ModeOf(r: RobotLike): Tdm107Mode | null {
  return r ? (tdm107IsSimRobot(r) ? 'sim' : 'real') : null;
}

/** Ids of the sim robots among `robots` (labels a row by its own robot_id). */
export function tdm107SimIds<R extends { id: bigint; cameraConfigJson: string }>(robots: readonly R[]): Set<bigint> {
  return new Set(robots.filter((r) => tdm107IsSimRobot(r)).map((r) => r.id));
}

export const TDM107_MODE_LABEL: Record<Tdm107Mode, string> = {
  sim: 'Sim robot (3D scene)',
  real: 'Real robot',
};

export const TDM107_MODE_HINT: Record<Tdm107Mode, string> = {
  sim: 'YAM in its 3D scene.',
  real: 'The YAM arm.',
};

/** The default for a new ticket: the sim robot. Evals on the real arm need a deployed policy, so a real arm
 * that is online is offered as the data-collection robot (TDM-125) instead of becoming the default eval robot. */
export function tdm107DefaultMode(_onlineNonTest: RobotLike[]): Tdm107Mode {
  return 'sim';
}

/** TDM-125 default: collect on the one online real arm, if there is exactly one. '' = same robot as the evals. */
export function tdm125DefaultCollectRobot<R extends RobotLike & { id: unknown }>(onlineNonTest: R[]): string {
  const real = onlineNonTest.filter((r): r is R => !!r && !tdm107IsSimRobot(r));
  return real.length === 1 ? String(real[0].id) : '';
}

function cfgOf(r: RobotLike): Record<string, unknown> {
  if (!r || !r.cameraConfigJson) return {};
  try {
    const cfg = JSON.parse(r.cameraConfigJson) as unknown;
    return cfg && typeof cfg === 'object' ? (cfg as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

/** Same rule as the orchestrator's `robot_can_eval`: sim robots can; a real robot only with `policy: true`.
 * A real robot with `teleop: true` and no `policy` flag is teleop-only (tonight's tdm119_teleop on yam-01).
 * A real robot with neither flag (older clients) keeps evals. */
export function tdm107CanEval(r: RobotLike): boolean {
  if (!r) return false;
  const cfg = cfgOf(r);
  if (cfg.sim === true) return true;
  if (typeof cfg.policy === 'boolean') return cfg.policy;
  return cfg.teleop !== true;
}

/** A real robot that can collect data (teleop) but can't run evals. */
export function tdm107IsTeleopOnly(r: RobotLike): boolean {
  return !!r && !tdm107CanEval(r);
}

/** The one-line field the form shows when a Real ticket on a teleop-only arm becomes a split (the server does the split). */
export function tdm125SplitLine(simName: string, realName: string): string {
  return `Evals: ${simName} · Data: ${realName}`;
}
