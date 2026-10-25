// TDM-86: the phone cockpit's logic (teleop + practice on a <=430 px screen, or a phone in landscape).
// Pure functions only (node --test loads this file by URL): touch wording for the coach, the
// gripper state line, the height readout, and the teleop coach input built from a sim robot's state.

import type { V3 } from './tdm56Input';
import type { Tdm106GuideInput } from './tdm106Guide';

/** The view the phone opens on: high above and behind the base, looking down the table, so pushing
 *  the Move stick up moves the hand up the screen (same camera as practice's driver's view). */
export const TDM86_DRIVER_CAM = { id: 'tdm106-driver', pos: [0.04, 0, 1.0] as V3, target: [0.34, 0, 0.0] as V3, fovy: 44 };

export interface Tdm86View {
  id: string;
  label: string;
}

/** The phone's view switcher, in order. Driver first; Side shows the hand's height against the cube. */
export const TDM86_VIEWS: readonly Tdm86View[] = [
  { id: TDM86_DRIVER_CAM.id, label: 'Driver' },
  { id: 'top', label: 'Top' },
  { id: 'side', label: 'Side' },
  { id: 'wrist', label: 'Wrist' },
];

/**
 * The coach's keyboard words, rewritten for thumbs: "Hold Q" -> "Push Up/down up", "(W)" dropped
 * (the direction word stays), "Press G" -> "Tap Grip".
 */
export function tdm86TouchWords(text: string): string {
  return text
    .replace(/Hold W A S D to move, Q and E to go up and down\./g, 'Drag the Move stick. The Up/down stick lifts and lowers.')
    .replace(/Lift first \(Q\)/g, 'Lift first (Up/down stick up)')
    .replace(/\bHold Q\b/g, 'Push the Up/down stick up')
    .replace(/\bHold E\b/g, 'Pull the Up/down stick down')
    .replace(/\bPress G\b/g, 'Tap Grip')
    .replace(/\bpress G\b/g, 'tap Grip')
    .replace(/\s*\((?:W|A|S|D|Q|E|G)\)/g, '');
}

export type Tdm86GripTone = 'holding' | 'open' | 'closed' | 'moving';

/** One word or two for the gripper, from the measured opening (0 closed .. 1 open) and the command. */
export function tdm86GripState(g: { opening: number | null; cmd: number; holding: boolean; objName?: string }): { text: string; tone: Tdm86GripTone } {
  const obj = g.objName ?? 'cube';
  if (g.holding) return { text: `Holding ${obj}`, tone: 'holding' };
  const open = g.opening ?? g.cmd;
  if (Math.abs(open - g.cmd) > 0.08) return { text: g.cmd > open ? 'Opening…' : 'Closing…', tone: 'moving' };
  return open > 0.5 ? { text: 'Gripper open', tone: 'open' } : { text: 'Gripper closed, empty', tone: 'closed' };
}

/** Height line: how far the hand is above the cube when it is over it, else above the table. */
export function tdm86HeightText(tool: V3, tableZ: number, obj: { pos: V3; half: V3; held: boolean } | null, objName = 'cube'): string {
  const cm = (m: number) => (Math.abs(m) < 0.1 ? `${(m * 100).toFixed(1)} cm` : `${Math.round(m * 100)} cm`);
  if (obj && !obj.held && Math.hypot(tool[0] - obj.pos[0], tool[1] - obj.pos[1]) < 0.06) {
    const dz = tool[2] - obj.pos[2];
    return dz > 0.004 ? `${cm(dz)} above the ${objName}` : `At the ${objName}`;
  }
  return `Hand ${cm(Math.max(0, tool[2] - tableZ))} above the table`;
}

/** Live poses as the sim robot publishes them on /ws/state (`scene.cube` / `scene.plate`). */
export interface Tdm86SceneLive {
  cube?: { pos: V3; size?: number };
  plate?: { pos: V3; radius?: number };
}

/**
 * Coach input for teleop on a sim robot: the cube and plate from the state feed, the tool point
 * from the twin's kinematics. "Held" is inferred (the robot does not say): fingers closed past half
 * and the cube within 3.5 cm of the tool point.
 */
export function tdm86TeleopGuideInput(tool: V3, gripper: number, cmd: number, scene: Tdm86SceneLive | null | undefined): Tdm106GuideInput | null {
  const cube = scene?.cube;
  if (!cube?.pos) return null;
  const half = (cube.size ?? 0.04) / 2;
  const d = Math.hypot(tool[0] - cube.pos[0], tool[1] - cube.pos[1], tool[2] - cube.pos[2]);
  const held = gripper < 0.6 && cmd < 0.5 && d < 0.035;
  const plate = scene?.plate;
  const goal = plate?.pos ? { pos: plate.pos, radius: plate.radius ?? 0.075 } : null;
  const onGoal = !!goal && !held && Math.hypot(cube.pos[0] - goal.pos[0], cube.pos[1] - goal.pos[1]) < goal.radius && cube.pos[2] < goal.pos[2] + half + 0.02;
  return {
    tool,
    gripperOpen: cmd > 0.5,
    gripperMoving: Math.abs(gripper - cmd) > 0.05 && !held,
    obj: { pos: cube.pos, half: [half, half, half], held },
    goal,
    done: onGoal,
    objName: 'cube',
    goalName: 'plate',
  };
}

/**
 * Tool height for the landing ghost ("where the hand ends up if you lower straight down"): the
 * cube's centre when the hand is over it; carrying it, the height that sets it down on the plate
 * (or the table); else just above the table.
 */
export function tdm86LandingZ(
  tool: V3,
  tableZ: number,
  obj: { pos: V3; half: V3; held: boolean } | null,
  goal: { pos: V3; radius: number } | null,
): number {
  if (obj?.held) {
    const overGoal = !!goal && Math.hypot(tool[0] - goal.pos[0], tool[1] - goal.pos[1]) < goal.radius;
    const surface = overGoal ? Math.max(tableZ, goal!.pos[2] + 0.006) : tableZ;
    const underside = obj.pos[2] - obj.half[2];
    return tool[2] - (underside - surface);
  }
  if (obj && Math.hypot(tool[0] - obj.pos[0], tool[1] - obj.pos[1]) <= 0.06) return obj.pos[2];
  return tableZ + 0.02;
}
