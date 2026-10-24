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
  sim: 'The MuJoCo YAM in its deployed scene. Practice, sim data and sim evals. Labelled SIMULATED everywhere.',
  real: 'The Jetson YAM. Only real-robot results count toward real success numbers.',
};

/** The default for a new ticket: the real arm when one is online, else the sim robot. */
export function tdm107DefaultMode(onlineNonTest: RobotLike[]): Tdm107Mode {
  return onlineNonTest.some((r) => r && !tdm107IsSimRobot(r)) ? 'real' : 'sim';
}
