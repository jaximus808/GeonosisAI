'use client';

// TDM-106: one input loop for both the teleop console (TDM-56) and practice (TDM-59), so practising
// is practising the real controls. Keyboard, gamepad, on-screen sticks, the on-screen clutch and the
// gripper all feed one ~30 Hz loop that turns them into TDM-56 contract commands
// ({clutch, d_pos, d_rot, gripper, speed_scale}: deltas from the pose at clutch-down).
//
// The consoles keep what differs: where commands go (`send`: the /control socket behind the TDM-57
// lease gate, or the practice sim behind a simulated network delay), what blocks driving
// (`blockReason`), and STOP (`pressStop`). Driving rules (tdm106Drive.ts): keys drive directly,
// translation only unless "Turn the hand" is on, slow default speed with a smooth ramp, Shift for
// precise moves, G for the gripper (with a short clutch pulse so the robot applies it), optional
// snap-to-grasp when the object pose is known.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { Tdm56StickValue } from '@/components/teleop/Tdm56Joystick';
import { Tdm56ClutchIntegrator, ZERO_TWIST, addTwist, deadzone, firstPad, readPad, round, type Tdm56Twist, type V3 } from '@/lib/teleop/tdm56Input';
import {
  TDM106_DEFAULT_SPEED,
  TDM106_GRIP_KEY,
  TDM106_KEYS,
  TDM106_PRECISE,
  Tdm106Gripper,
  Tdm106Ramp,
  tdm106KeyTwist,
  tdm106KeysWantDrive,
  tdm106Snap,
  tdm106TranslationOnly,
  type Tdm106GripMode,
} from '@/lib/teleop/tdm106Drive';

const TICK_MS = 33; // ~30 Hz
const GRIP_RATE = 0.9; // per second, for held R / F keys and bumpers

export type Tdm106Block = { code: string; text: string } | null;

export interface Tdm106DriveCmd {
  seq: number;
  clutch: boolean;
  d_pos: V3;
  d_rot: V3;
  gripper: number;
  speed_scale: number;
}

export interface Tdm106Settings {
  speed: number;
  /** Arrow keys / right stick X / d-pad turn and tilt the hand. Off = translation only. */
  rotation: boolean;
  /** Holding a move key is the clutch (no Space needed). */
  directKeys: boolean;
  gripMode: Tdm106GripMode;
  /** Snap-to-grasp assist (only where the object pose is known). */
  snap: boolean;
  /** Touching an on-screen stick holds the clutch. */
  stickEngages: boolean;
}

export const TDM106_DEFAULT_SETTINGS: Tdm106Settings = {
  speed: TDM106_DEFAULT_SPEED,
  rotation: false,
  directKeys: true,
  gripMode: 'toggle',
  snap: true,
  stickEngages: true,
};

export interface Tdm106DriveOptions {
  blockReason: () => Tdm106Block;
  /** Send one command. Return false when a gate refused it (the clutch must then be pressed again).
   *  `release` = the final clutch:false that makes the arm hold at once. */
  send: (cmd: Tdm106DriveCmd, release: boolean) => boolean;
  pressStop: () => void;
  /** Snap assist: tool and object positions in the base frame, when known (sim scenes only). */
  assistTarget?: () => { tool: V3; obj: V3 } | null;
  /** After each tick (practice: deliver delayed commands, step the sim). */
  afterTick?: (t: { now: number; dt: number; engaged: boolean }) => void;
  /** Ignore keys while true (e.g. a walkthrough is open). */
  keysPaused?: () => boolean;
  defaults?: Partial<Tdm106Settings>;
}

export interface Tdm106DriveUi {
  engaged: boolean;
  want: boolean;
  needsRelease: boolean;
  block: Tdm106Block;
  /** Which input holds the clutch: keys | keyboard | gamepad | clutch button | stick | gripper. */
  source: string | null;
  pad: string | null;
  gripper: number;
  dPos: V3;
  dRot: V3;
  /** Motion commands sent in the last second. */
  hz: number;
}

/** Codes shown on the legend that are not motion keys. */
const LEGEND_EXTRA = new Set(['Space', 'Escape', 'ShiftLeft', 'ShiftRight']);

export function useTdm106Drive(opts: Tdm106DriveOptions) {
  const o = useRef(opts);
  o.current = opts;

  const [settings, setSettings] = useState<Tdm106Settings>({ ...TDM106_DEFAULT_SETTINGS, ...opts.defaults });
  const sRef = useRef(settings);
  sRef.current = settings;
  const set = useCallback((p: Partial<Tdm106Settings>) => setSettings((s) => ({ ...s, ...p })), []);

  // ---- inputs (refs: read by the loop without re-rendering)
  const keys = useRef(new Set<string>());
  const [held, setHeld] = useState<ReadonlySet<string>>(new Set());
  const shift = useRef(false);
  const spaceHeld = useRef(false);
  const clutchBtn = useRef(false);
  const stickL = useRef<Tdm56StickValue>({ x: 0, y: 0, active: false });
  const stickR = useRef<Tdm56StickValue>({ x: 0, y: 0, active: false });
  const grip = useRef(new Tdm106Gripper());
  grip.current.mode = settings.gripMode;
  const [gripper, setGripperState] = useState(1);
  const syncGrip = () => setGripperState(grip.current.value);

  const setGripper = useCallback((v: number) => {
    grip.current.set(v, performance.now());
    syncGrip();
  }, []);
  const toggleGripper = useCallback(() => {
    grip.current.toggle(performance.now());
    syncGrip();
  }, []);

  // ---- loop state
  const integ = useRef(new Tdm56ClutchIntegrator());
  const ramp = useRef(new Tdm106Ramp());
  const engaged = useRef(false);
  const needsRelease = useRef(false);
  const seq = useRef(0);
  const lastTick = useRef(0);
  const padPrev = useRef({ a: false, b: false });
  const lastBlock = useRef<Tdm106Block>(null);
  const lastSource = useRef<string | null>(null);
  const lastPad = useRef<string | null>(null);
  const lastWant = useRef(false);
  const sentTimes = useRef<number[]>([]);

  const cmd = (clutch: boolean): Tdm106DriveCmd => ({
    seq: ++seq.current,
    clutch,
    d_pos: integ.current.dPos.map((v) => round(v)) as V3,
    d_rot: integ.current.dRot.map((v) => round(v)) as V3,
    gripper: round(grip.current.value, 3),
    speed_scale: round(sRef.current.speed, 3),
  });

  const release = useCallback(() => {
    if (!engaged.current) return;
    engaged.current = false;
    o.current.send(cmd(false), true);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    lastTick.current = performance.now();
    const id = window.setInterval(() => {
      const now = performance.now();
      const dt = Math.min(0.1, (now - lastTick.current) / 1000);
      lastTick.current = now;
      const s = sRef.current;
      const g = grip.current;

      // gamepad
      const p = firstPad();
      const pad = p ? readPad(p) : null;
      lastPad.current = pad?.id ?? null;
      if (pad) {
        if (pad.pressedB && !padPrev.current.b) o.current.pressStop();
        if (pad.pressedA && !padPrev.current.a) {
          g.toggle(now);
          syncGrip();
        }
        padPrev.current = { a: pad.pressedA, b: pad.pressedB };
      }
      // gradual gripper (R / F, bumpers)
      let gRate = pad?.gripperRate ?? 0;
      if (keys.current.has('KeyR')) gRate += 1;
      if (keys.current.has('KeyF')) gRate -= 1;
      if (gRate) {
        g.set(g.value + Math.sign(gRate) * GRIP_RATE * dt, now);
        syncGrip();
      }

      // velocity from every source
      let twist: Tdm56Twist = tdm106KeyTwist(keys.current, { rotation: s.rotation, shift: shift.current });
      if (pad) twist = addTwist(twist, tdm106TranslationOnly(pad.twist, s.rotation));
      const L = stickL.current;
      const R = stickR.current;
      if (L.active || R.active) {
        twist = addTwist(twist, { lin: [deadzone(L.y), -deadzone(L.x), deadzone(R.y)], ang: [0, 0, s.rotation ? -deadzone(R.x) : 0] });
      }

      const sources: string[] = [];
      if (s.directKeys && tdm106KeysWantDrive(keys.current, s.rotation)) sources.push('keys');
      if (pad?.clutch) sources.push('gamepad');
      if (spaceHeld.current) sources.push('keyboard');
      if (clutchBtn.current) sources.push('clutch button');
      if (s.stickEngages && (L.active || R.active)) sources.push('stick');
      if (g.pulse(now)) sources.push('gripper');
      const want = sources.length > 0;
      lastWant.current = want;
      lastSource.current = sources[0] ?? null;

      const block = o.current.blockReason();
      lastBlock.current = block;
      if (block) {
        release();
        if (want) needsRelease.current = true;
      } else if (!want) {
        needsRelease.current = false;
        release();
      } else if (!needsRelease.current) {
        if (!engaged.current) {
          integ.current.reset(); // deltas are from the pose at clutch-down
          ramp.current.reset(); // and every stroke eases in
          engaged.current = true;
        }
        let tw = ramp.current.step(twist, dt);
        // snap-to-grasp: only while the user is actually moving the hand
        const moving = Math.hypot(...twist.lin) > 0.05;
        const at = s.snap && moving ? o.current.assistTarget?.() : null;
        if (at) {
          const [sx, sy] = tdm106Snap(at.tool, at.obj);
          const k = shift.current ? TDM106_PRECISE : 1;
          tw = { lin: [tw.lin[0] + sx * k, tw.lin[1] + sy * k, tw.lin[2]], ang: tw.ang };
        }
        integ.current.step(tw ?? ZERO_TWIST, dt, s.speed);
        if (o.current.send(cmd(true), false)) sentTimes.current.push(now);
        else {
          // a gate said no at this instant (lease running low, tab hidden): let go, press again later
          release();
          needsRelease.current = true;
        }
      }
      const st = sentTimes.current;
      while (st.length && now - st[0] > 1000) st.shift();
      o.current.afterTick?.({ now, dt, engaged: engaged.current });
    }, TICK_MS);
    return () => {
      window.clearInterval(id);
      release();
    };
  }, [release]);

  // ---- keyboard
  useEffect(() => {
    const typing = (t: EventTarget | null) =>
      t instanceof HTMLElement && (t.tagName === 'TEXTAREA' || (t.tagName === 'INPUT' && (t as HTMLInputElement).type !== 'range' && (t as HTMLInputElement).type !== 'checkbox') || t.isContentEditable);
    const showHeld = () => setHeld(new Set([...keys.current, ...(spaceHeld.current ? ['Space'] : []), ...(shift.current ? ['ShiftLeft'] : [])]));
    const down = (e: KeyboardEvent) => {
      if (typing(e.target) || o.current.keysPaused?.()) return;
      shift.current = e.shiftKey;
      if (e.code === 'Escape') {
        e.preventDefault();
        o.current.pressStop();
        setHeld((h) => new Set([...h, 'Escape']));
        return;
      }
      if (e.code === 'Space') {
        e.preventDefault();
        spaceHeld.current = true;
        showHeld();
        return;
      }
      if (e.code === TDM106_GRIP_KEY) {
        e.preventDefault();
        if (!e.repeat) {
          grip.current.keyDown(performance.now());
          syncGrip();
        }
      }
      if (TDM106_KEYS.has(e.code)) {
        e.preventDefault();
        keys.current.add(e.code);
      }
      showHeld();
    };
    const up = (e: KeyboardEvent) => {
      shift.current = e.shiftKey;
      if (e.code === 'Space') {
        e.preventDefault(); // a focused button would otherwise "click" on Space release
        spaceHeld.current = false;
      }
      if (e.code === TDM106_GRIP_KEY && keys.current.has(TDM106_GRIP_KEY)) {
        grip.current.keyUp(performance.now());
        syncGrip();
      }
      keys.current.delete(e.code);
      if (e.code === 'Escape') setHeld((h) => new Set([...h].filter((c) => c !== 'Escape')));
      else showHeld();
    };
    const drop = () => {
      keys.current.clear();
      spaceHeld.current = false;
      shift.current = false;
      clutchBtn.current = false;
      stickL.current = { x: 0, y: 0, active: false };
      stickR.current = { x: 0, y: 0, active: false };
      if (grip.current.mode === 'hold' && !grip.current.open) {
        grip.current.keyUp(performance.now());
        syncGrip();
      }
      setHeld(new Set());
    };
    const vis = () => document.hidden && drop();
    window.addEventListener('keydown', down, true);
    window.addEventListener('keyup', up, true);
    window.addEventListener('blur', drop);
    document.addEventListener('visibilitychange', vis);
    return () => {
      window.removeEventListener('keydown', down, true);
      window.removeEventListener('keyup', up, true);
      window.removeEventListener('blur', drop);
      document.removeEventListener('visibilitychange', vis);
    };
  }, []);

  // ---- UI snapshot at ~10 Hz
  const [ui, setUi] = useState<Tdm106DriveUi>({
    engaged: false,
    want: false,
    needsRelease: false,
    block: null,
    source: null,
    pad: null,
    gripper: 1,
    dPos: [0, 0, 0],
    dRot: [0, 0, 0],
    hz: 0,
  });
  useEffect(() => {
    const id = window.setInterval(() => {
      setUi({
        engaged: engaged.current,
        want: lastWant.current,
        needsRelease: needsRelease.current,
        block: lastBlock.current,
        source: lastSource.current,
        pad: lastPad.current,
        gripper: grip.current.value,
        dPos: integ.current.dPos,
        dRot: integ.current.dRot,
        hz: sentTimes.current.length,
      });
    }, 100);
    return () => window.clearInterval(id);
  }, []);

  const clutch = useMemo(
    () => ({
      down: () => {
        clutchBtn.current = true;
      },
      up: () => {
        clutchBtn.current = false;
      },
    }),
    [],
  );
  const sticks = useMemo(
    () => ({
      left: (v: Tdm56StickValue) => {
        stickL.current = v;
      },
      right: (v: Tdm56StickValue) => {
        stickR.current = v;
      },
    }),
    [],
  );

  /** Forget every held input (used on reset / STOP). */
  const resetInputs = useCallback(() => {
    integ.current.reset();
    ramp.current.reset();
  }, []);

  return { ui, held, legendExtra: LEGEND_EXTRA, settings, set, gripper, setGripper, toggleGripper, clutch, sticks, release, resetInputs };
}

export type Tdm106Drive = ReturnType<typeof useTdm106Drive>;
