// TDM-106: the live coach for pick-and-place in practice. Given where the hand, the object and the
// goal are, it says the ONE next thing to do in plain words, with the keys for it, so a first-time
// operator never has to work out the controls from a table. Pure logic (node --test loads it by URL).

import type { V3 } from './tdm56Input';

// Same captions as TDM106_MOVE_KEYS in tdm106Drive.ts (kept local: no runtime imports here).
const MOVE: Record<string, { cap: string; word: string }> = {
  KeyW: { cap: 'W', word: 'forward' },
  KeyS: { cap: 'S', word: 'back' },
  KeyA: { cap: 'A', word: 'left' },
  KeyD: { cap: 'D', word: 'right' },
};

function tdm106Cm(m: number): string {
  const cm = Math.abs(m) * 100;
  return cm < 1 ? `${cm.toFixed(1)} cm` : `${Math.round(cm)} cm`;
}

export interface Tdm106GuideInput {
  tool: V3;
  gripperOpen: boolean;
  /** The jaws are still travelling to the commanded opening. */
  gripperMoving?: boolean;
  obj: { pos: V3; half: V3; held: boolean } | null;
  goal: { pos: V3; radius: number } | null;
  done: boolean;
  /** Names for the hint text, e.g. "red cube", "plate". */
  objName?: string;
  goalName?: string;
}

export type Tdm106GuideStep = 'open' | 'over_obj' | 'lower' | 'close' | 'lift' | 'carry' | 'release' | 'done';

export interface Tdm106Guide {
  step: Tdm106GuideStep;
  /** Step number 1..5 of the task, for the progress dots. */
  n: number;
  title: string;
  detail: string;
  /** Key codes to highlight on the legend. */
  keys: string[];
}

/** Sideways tolerance for "over it" (m) and the carry height that clears the scene. */
const ALIGN = 0.012;
const CARRY_Z = 0.07;

/** "forward 6 cm (W), right 3 cm (D)" plus the keys, for a sideways offset in the base frame. */
export function tdm106Directions(dx: number, dy: number, tol = ALIGN): { text: string; keys: string[] } {
  const parts: string[] = [];
  const keys: string[] = [];
  const add = (v: number, pos: string, neg: string) => {
    if (Math.abs(v) <= tol * 0.7) return;
    const code = v > 0 ? pos : neg;
    const k = MOVE[code];
    parts.push(`${k.word} ${tdm106Cm(v)} (${k.cap})`);
    keys.push(code);
  };
  add(dx, 'KeyW', 'KeyS');
  add(dy, 'KeyA', 'KeyD');
  return { text: parts.join(', '), keys };
}

export function tdm106Guide(g: Tdm106GuideInput): Tdm106Guide {
  const obj = g.objName ?? 'cube';
  const goal = g.goalName ?? 'plate';
  if (g.done) return { step: 'done', n: 5, title: `Done: the ${obj} is on the ${goal}`, detail: 'That is the whole task. Reset to go again, or try it faster.', keys: [] };
  if (!g.obj) return { step: 'over_obj', n: 1, title: 'Move the hand', detail: 'Hold W A S D to move, Q and E to go up and down.', keys: ['KeyW', 'KeyA', 'KeyS', 'KeyD'] };

  const t = g.tool;
  if (g.obj.held) {
    if (!g.goal) return { step: 'release', n: 5, title: 'Put it down', detail: 'Press G to open the gripper.', keys: ['KeyG'] };
    const dx = g.goal.pos[0] - t[0];
    const dy = g.goal.pos[1] - t[1];
    const off = Math.hypot(dx, dy);
    const objZ = g.obj.pos[2] - g.obj.half[2]; // underside of the object above the table
    if (off > g.goal.radius * 0.6) {
      if (objZ < CARRY_Z * 0.5 && off > g.goal.radius) {
        return { step: 'lift', n: 3, title: 'Lift it', detail: `Hold Q to raise it about ${tdm106Cm(CARRY_Z - objZ)} so it clears the table.`, keys: ['KeyQ'] };
      }
      const d = tdm106Directions(dx, dy, g.goal.radius * 0.4);
      return { step: 'carry', n: 4, title: `Carry it over the ${goal}`, detail: `Move ${d.text}.`, keys: d.keys };
    }
    if (objZ > 0.04) return { step: 'release', n: 5, title: `Lower it onto the ${goal}`, detail: `Hold E to lower it (${tdm106Cm(objZ - 0.015)} to go), then press G to let go.`, keys: ['KeyE', 'KeyG'] };
    return { step: 'release', n: 5, title: 'Let go', detail: 'Press G to open the gripper.', keys: ['KeyG'] };
  }

  const dx = g.obj.pos[0] - t[0];
  const dy = g.obj.pos[1] - t[1];
  const off = Math.hypot(dx, dy);
  const dz = t[2] - g.obj.pos[2];
  if (!g.gripperOpen && g.gripperMoving && off <= 0.03) return { step: 'close', n: 2, title: 'Closing…', detail: 'The fingers are closing around it.', keys: [] };
  if (!g.gripperOpen && off > 0.03) return { step: 'open', n: 1, title: 'Open the gripper', detail: 'Press G. The fingers open wide.', keys: ['KeyG'] };
  if (off > ALIGN) {
    const d = tdm106Directions(dx, dy);
    const low = dz < 0.04 && off > 0.04;
    return {
      step: 'over_obj',
      n: 1,
      title: `Line up over the ${obj}`,
      detail: low ? `Lift first (Q), then move ${d.text}.` : `Move ${d.text}. The blue ghost under the hand shows where it lands.`,
      keys: low ? ['KeyQ', ...d.keys] : d.keys,
    };
  }
  if (!g.gripperOpen) return { step: 'open', n: 1, title: 'Open the gripper', detail: 'Press G, then lower onto it.', keys: ['KeyG'] };
  if (dz > Math.max(0.012, g.obj.half[2] * 0.5)) return { step: 'lower', n: 2, title: `Lower onto the ${obj}`, detail: `Hold E (${tdm106Cm(dz)} to go). The fingers go around it.`, keys: ['KeyE'] };
  return { step: 'close', n: 2, title: 'Grab it', detail: 'Press G to close the gripper.', keys: ['KeyG'] };
}
