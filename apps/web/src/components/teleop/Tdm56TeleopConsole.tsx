'use client';

// TDM-56: the teleop console mounted by /teleop/[taskId].
//
// Drives the arm over the TDM-55 stream server's `/control` WebSocket (lib/teleop/tdm56ControlClient.ts)
// with clutch control: motion is sent ONLY while a clutch is held (gamepad RT, Space, the on-screen
// clutch button, or touching an on-screen stick), as deltas from the pose at clutch-down, ~30 Hz.
//
// The clutch is forced off (and must be let go and pressed again) when any of these is true:
//   camera feed stale or delayed past `maxLatencyMs`, control link not open, robot state silent > 1 s,
//   robot reports a latched stop, STOP pressed here, `canDrive` false (TDM-57 lease), tab hidden/blurred.
//
// Slots for parallel tickets:
//   canDrive / canDriveReason  TDM-57 exclusive control (lease heartbeat, lost control)
//   controlSlot                TDM-57 lease / claim status, rendered in the top bar
//   recordingSlot              TDM-58 record start/stop, takeover/handback; use useTdm56Teleop().controlSocket
//   stamp                      TDM-57 command gate (useTeleopLease().stamp): every outgoing cmd and recording op
//                              goes through it; null = not allowed now, nothing is sent

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { CameraFeed, TaskCameraFeed, type FeedHealth } from '@/components/teleop/CameraFeed';
import { Tdm56Joystick, type Tdm56StickValue } from '@/components/teleop/Tdm56Joystick';
import { useContributor } from '@/lib/contributor/hooks';
import { staticStreamGrant, taskStreamGrant } from '@/lib/teleop/tdm55StreamGrant';
import type { StreamGrant } from '@/lib/teleop/tdm55CameraClient';
import type { ControlChannel, RobotControlMessage } from '@/lib/teleop/tdm58Recording';
import {
  Tdm56ControlClient,
  type Tdm56LinkSnapshot,
  type Tdm56OutMsg,
  type Tdm56Cmd,
  type Tdm56RobotState,
} from '@/lib/teleop/tdm56ControlClient';
import {
  Tdm56ClutchIntegrator,
  TDM56_KEYS,
  ZERO_TWIST,
  addTwist,
  deadzone,
  firstPad,
  keyboardTwist,
  readPad,
  round,
  type Tdm56Twist,
  type V3,
} from '@/lib/teleop/tdm56Input';

const TICK_MS = 33; // ~30 Hz
const ROBOT_SILENT_MS = 1000;
const SLOW_CAP = 0.25;
const GRIP_RATE = 0.9; // per second, for held gripper keys / bumpers

// ------------------------------------------------------------------ context for TDM-57 / TDM-58

export interface Tdm56TeleopApi {
  /** Send a non-motion contract message: {t:"record_start"}, {t:"record_stop", success}, {t:"takeover"}, {t:"handback"}, {t:"stop"}. */
  send: (msg: Exclude<Tdm56OutMsg, Tdm56Cmd>) => boolean;
  link: Tdm56LinkSnapshot;
  robot: Tdm56RobotState | null;
  /** True while motion commands are going out. */
  driving: boolean;
  /** TDM-58 `<RecordingControls controlSocket/>`: the same /control socket, stable across reconnects. Sends are stamped. */
  controlSocket: ControlChannel;
}

const Tdm56Ctx = createContext<Tdm56TeleopApi | null>(null);

/** For components mounted in the console's slots (TDM-57 / TDM-58). */
export function useTdm56Teleop(): Tdm56TeleopApi {
  const v = useContext(Tdm56Ctx);
  if (!v) throw new Error('useTdm56Teleop must be used inside <Tdm56TeleopConsole>');
  return v;
}

// ------------------------------------------------------------------ props

export interface Tdm56TeleopConsoleProps {
  taskId: string;
  /** TDM-57: false while this browser does not hold a live control lease. Forces the clutch off. Default true. */
  canDrive?: boolean;
  canDriveReason?: string;
  controlSlot?: ReactNode;
  recordingSlot?: ReactNode;
  /** TDM-57 lease gate (useTeleopLease().stamp). Adds {task_id, claimed_at_us} to every cmd / op, or returns null
   *  when control is not allowed at this instant (then nothing is sent). STOP is never gated. */
  stamp?: <T extends object>(msg: T) => (T & { task_id: number; claimed_at_us: number }) | null;
  /** Force the clutch off when the worst camera latency exceeds this. Default 400 ms. */
  maxLatencyMs?: number;
  /** Dev / mock robot: a fixed stream URL + token instead of the claimant grant. Shown as such in the UI. */
  dev?: { streamUrl: string; token: string; controlUrl?: string };
}

type Block = { code: string; text: string } | null;

interface UiSnap {
  engaged: boolean;
  wantClutch: boolean;
  needsRelease: boolean;
  block: Block;
  gripper: number;
  dPos: V3;
  dRot: V3;
  pad: string | null;
  source: string | null; // which input holds the clutch
}

const fmt = (v: number | undefined, d = 1, k = 1) => (v == null ? '--' : (v * k).toFixed(d));

export function Tdm56TeleopConsole(props: Tdm56TeleopConsoleProps) {
  const { identity } = useContributor();
  const { taskId, dev } = props;
  const getGrant = useMemo<(() => Promise<StreamGrant>) | null>(() => {
    if (dev) return staticStreamGrant(dev.streamUrl, dev.token);
    return identity ? taskStreamGrant(taskId, identity) : null;
  }, [dev, taskId, identity]);

  if (!getGrant) {
    return <p className="rounded-lg bg-zinc-900 p-6 text-center text-sm text-zinc-300">Connecting to the factory…</p>;
  }
  return <ConsoleInner key={`${taskId}:${identity ?? 'dev'}`} {...props} getGrant={getGrant} />;
}

function ConsoleInner({
  taskId,
  canDrive = true,
  canDriveReason,
  controlSlot,
  recordingSlot,
  maxLatencyMs = 400,
  dev,
  stamp,
  getGrant,
}: Tdm56TeleopConsoleProps & { getGrant: () => Promise<StreamGrant> }) {
  const stampRef = useRef(stamp);
  stampRef.current = stamp;
  /** Lease-stamp a message; unstamped when no gate is mounted (dev page). */
  const gate = useCallback(<T extends object>(m: T): T | null => (stampRef.current ? stampRef.current(m) : m), []);
  // robot -> browser messages for slot components (TDM-58), re-attached to every new control client
  const subs = useRef(new Set<(m: RobotControlMessage) => void>());
  // ---- link
  const [link, setLink] = useState<Tdm56LinkSnapshot>({
    state: 'idle',
    cmdHz: 0,
    ackRttMs: null,
    lastAckAt: null,
    lastStateAt: null,
    robot: null,
    stopPending: false,
  });
  const clientRef = useRef<Tdm56ControlClient | null>(null);
  const linkRef = useRef(link);
  linkRef.current = link;
  const grantRef = useRef(getGrant);
  grantRef.current = getGrant;

  useEffect(() => {
    const c = new Tdm56ControlClient(() => grantRef.current(), setLink, { urlOverride: dev?.controlUrl });
    clientRef.current = c;
    const unsub = c.subscribe((m) => {
      for (const fn of subs.current) fn(m as RobotControlMessage);
    });
    c.start();
    return () => {
      unsub();
      c.stop();
      clientRef.current = null;
    };
  }, [dev?.controlUrl]);

  // ---- inputs (refs: read by the 30 Hz loop without re-rendering)
  const feedRef = useRef<FeedHealth | null>(null);
  const [feed, setFeed] = useState<FeedHealth | null>(null);
  const onHealth = useCallback((h: FeedHealth) => {
    feedRef.current = h;
    setFeed(h);
  }, []);

  const keys = useRef(new Set<string>());
  const shift = useRef(false);
  const spaceHeld = useRef(false);
  const clutchBtn = useRef(false);
  const stickL = useRef<Tdm56StickValue>({ x: 0, y: 0, active: false });
  const stickR = useRef<Tdm56StickValue>({ x: 0, y: 0, active: false });
  const [stickEngages, setStickEngages] = useState(true);
  const stickEngagesRef = useRef(stickEngages);
  stickEngagesRef.current = stickEngages;

  const [gripper, setGripperState] = useState(1); // 1 = open
  const gripperRef = useRef(1);
  const setGripper = (g: number) => {
    gripperRef.current = Math.max(0, Math.min(1, g));
    setGripperState(gripperRef.current);
  };

  const [speed, setSpeed] = useState(0.5);
  const [slow, setSlow] = useState(true);
  const effSpeed = slow ? Math.min(speed, SLOW_CAP) : speed;
  const effSpeedRef = useRef(effSpeed);
  effSpeedRef.current = effSpeed;

  const canDriveRef = useRef({ canDrive, canDriveReason });
  canDriveRef.current = { canDrive, canDriveReason };
  const maxLatRef = useRef(maxLatencyMs);
  maxLatRef.current = maxLatencyMs;

  // STOP pressed here: stays latched until the operator confirms resume.
  const [localStop, setLocalStop] = useState(false);
  const localStopRef = useRef(false);
  localStopRef.current = localStop;

  const [showSticks, setShowSticks] = useState(false);
  // two sticks + the clutch column must fit a 390 px phone (same sizes as TDM-59's practice console)
  const [stickSize, setStickSize] = useState(124);
  useEffect(() => {
    setShowSticks(window.matchMedia?.('(pointer: coarse)').matches ?? false);
    const fit = () => setStickSize(window.innerWidth < 420 ? 108 : 124);
    fit();
    window.addEventListener('resize', fit);
    return () => window.removeEventListener('resize', fit);
  }, []);

  const pressStop = useCallback(() => {
    localStopRef.current = true;
    setLocalStop(true);
    clientRef.current?.sendStop();
  }, []);

  // ---- loop state
  const integ = useRef(new Tdm56ClutchIntegrator());
  const engaged = useRef(false);
  const needsRelease = useRef(false);
  const seq = useRef(0);
  const lastTick = useRef(0);
  const padPrev = useRef({ a: false, b: false });
  const lastBlock = useRef<Block>(null);
  const lastSource = useRef<string | null>(null);
  const lastPad = useRef<string | null>(null);
  const lastWant = useRef(false);

  const blockReason = useCallback((): Block => {
    if (localStopRef.current) return { code: 'stop', text: 'Stopped from this page. Check the arm, then resume.' };
    const l = linkRef.current;
    const c = clientRef.current;
    if (!c?.isOpen) return { code: 'link', text: l.state === 'denied' ? `No control access: ${l.detail ?? 'not the claimant'}` : 'Control link is not connected.' };
    if (l.robot?.stopped) return { code: 'robot_stop', text: 'The robot is in a latched stop. Someone at the arm must clear it.' };
    const now = performance.now();
    const heard = Math.max(l.lastStateAt ?? 0, l.lastAckAt ?? 0);
    if (!l.lastStateAt) return { code: 'robot_wait', text: 'Waiting for the robot to report its state.' };
    if (now - heard > ROBOT_SILENT_MS) return { code: 'robot_silent', text: `No word from the robot for ${((now - heard) / 1000).toFixed(1)} s.` };
    if (!canDriveRef.current.canDrive) return { code: 'lease', text: canDriveRef.current.canDriveReason ?? 'You do not hold control of this robot.' };
    const f = feedRef.current;
    if (!f || f.stale) return { code: 'stale', text: 'Camera feed is stale. Driving is paused until the picture is live.' };
    if (f.worstLatencyMs != null && f.worstLatencyMs > maxLatRef.current)
      return { code: 'latency', text: `Video delay is ${Math.round(f.worstLatencyMs)} ms (limit ${maxLatRef.current} ms). Driving is paused.` };
    if (typeof document !== 'undefined' && document.hidden) return { code: 'hidden', text: 'Tab is in the background.' };
    return null;
  }, []);

  const release = useCallback(() => {
    if (!engaged.current) return;
    engaged.current = false;
    // One final clutch:false so the robot holds at once instead of waiting for its 200 ms watchdog.
    // Stamped when the lease allows it; otherwise sent bare (it can only make the arm hold).
    const rel = {
      seq: ++seq.current,
      clutch: false,
      d_pos: integ.current.dPos.map((v) => round(v)) as V3,
      d_rot: integ.current.dRot.map((v) => round(v)) as V3,
      gripper: round(gripperRef.current, 3),
      speed_scale: round(effSpeedRef.current, 3),
    };
    clientRef.current?.sendCmd(gate(rel) ?? rel);
  }, [gate]);

  // ---- the 30 Hz loop
  useEffect(() => {
    lastTick.current = performance.now();
    const id = window.setInterval(() => {
      const now = performance.now();
      const dt = Math.min(0.1, (now - lastTick.current) / 1000);
      lastTick.current = now;

      // gamepad
      const p = firstPad();
      const pad = p ? readPad(p) : null;
      lastPad.current = pad?.id ?? null;
      if (pad) {
        if (pad.pressedB && !padPrev.current.b) pressStop();
        if (pad.pressedA && !padPrev.current.a) setGripper(gripperRef.current > 0.5 ? 0 : 1);
        padPrev.current = { a: pad.pressedA, b: pad.pressedB };
      }

      // gripper ramps (keyboard F/R, bumpers)
      let gRate = pad?.gripperRate ?? 0;
      if (keys.current.has('KeyR')) gRate += 1;
      if (keys.current.has('KeyF')) gRate -= 1;
      if (gRate) setGripper(gripperRef.current + Math.sign(gRate) * GRIP_RATE * dt);

      // twist from every source
      let twist: Tdm56Twist = keyboardTwist(keys.current, shift.current);
      if (pad) twist = addTwist(twist, pad.twist);
      const L = stickL.current;
      const R = stickR.current;
      if (L.active || R.active) {
        twist = addTwist(twist, { lin: [deadzone(L.y), -deadzone(L.x), deadzone(R.y)], ang: [0, 0, -deadzone(R.x)] });
      }

      const sources: string[] = [];
      if (pad?.clutch) sources.push('gamepad');
      if (spaceHeld.current) sources.push('keyboard');
      if (clutchBtn.current) sources.push('clutch button');
      if (stickEngagesRef.current && (L.active || R.active)) sources.push('stick');
      const want = sources.length > 0;
      lastSource.current = sources[0] ?? null;
      lastWant.current = want;

      const block = blockReason();
      lastBlock.current = block;
      if (block) {
        release();
        if (want) needsRelease.current = true;
        return;
      }
      if (!want) {
        needsRelease.current = false;
        release();
        return;
      }
      if (needsRelease.current) return; // let go first, then press again

      if (!engaged.current) {
        integ.current.reset(); // deltas are from the pose at clutch-down
        engaged.current = true;
      }
      integ.current.step(twist ?? ZERO_TWIST, dt, effSpeedRef.current);
      const cmd = gate({
        seq: ++seq.current,
        clutch: true,
        d_pos: integ.current.dPos.map((v) => round(v)) as V3,
        d_rot: integ.current.dRot.map((v) => round(v)) as V3,
        gripper: round(gripperRef.current, 3),
        speed_scale: round(effSpeedRef.current, 3),
      });
      if (!cmd) {
        // the lease gate said no at this instant (lease running low, tab hidden): let go, press again later
        release();
        needsRelease.current = true;
        return;
      }
      clientRef.current?.sendCmd(cmd);
    }, TICK_MS);
    return () => {
      window.clearInterval(id);
      release();
    };
  }, [blockReason, release, pressStop, gate]);

  // ---- keyboard
  useEffect(() => {
    const typing = (t: EventTarget | null) =>
      t instanceof HTMLElement && (t.tagName === 'TEXTAREA' || (t.tagName === 'INPUT' && (t as HTMLInputElement).type !== 'range') || t.isContentEditable);
    const down = (e: KeyboardEvent) => {
      if (typing(e.target)) return;
      shift.current = e.shiftKey;
      if (e.code === 'Escape') {
        e.preventDefault();
        pressStop();
        return;
      }
      if (e.code === 'Space') {
        e.preventDefault();
        spaceHeld.current = true;
        return;
      }
      if (TDM56_KEYS[e.code] || e.code === 'KeyF' || e.code === 'KeyR') {
        e.preventDefault();
        keys.current.add(e.code);
      }
    };
    const up = (e: KeyboardEvent) => {
      shift.current = e.shiftKey;
      if (e.code === 'Space') {
        e.preventDefault(); // a focused button would otherwise "click" on Space release
        spaceHeld.current = false;
      }
      keys.current.delete(e.code);
    };
    const drop = () => {
      keys.current.clear();
      spaceHeld.current = false;
      clutchBtn.current = false;
      stickL.current = { x: 0, y: 0, active: false };
      stickR.current = { x: 0, y: 0, active: false };
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
  }, [pressStop]);

  // ---- UI snapshot at ~10 Hz
  const [ui, setUi] = useState<UiSnap>({
    engaged: false,
    wantClutch: false,
    needsRelease: false,
    block: null,
    gripper: 1,
    dPos: [0, 0, 0],
    dRot: [0, 0, 0],
    pad: null,
    source: null,
  });
  useEffect(() => {
    const id = window.setInterval(() => {
      setUi({
        engaged: engaged.current,
        wantClutch: lastWant.current,
        needsRelease: needsRelease.current,
        block: lastBlock.current,
        gripper: gripperRef.current,
        dPos: integ.current.dPos,
        dRot: integ.current.dRot,
        pad: lastPad.current,
        source: lastSource.current,
      });
    }, 100);
    return () => window.clearInterval(id);
  }, []);

  const controlSocket = useMemo<ControlChannel>(
    () => ({
      send: (m) => {
        if (m.t === 'stop') {
          pressStop();
          return clientRef.current?.isOpen ?? false;
        }
        const out = gate(m);
        return out ? (clientRef.current?.sendJson(out) ?? false) : false;
      },
      subscribe: (fn) => {
        subs.current.add(fn);
        return () => {
          subs.current.delete(fn);
        };
      },
    }),
    [pressStop, gate],
  );

  const api = useMemo<Tdm56TeleopApi>(
    () => ({
      send: (m) => controlSocket.send(m as Record<string, unknown>),
      link,
      robot: link.robot,
      driving: ui.engaged,
      controlSocket,
    }),
    [link, ui.engaged, controlSocket],
  );

  const robot = link.robot;
  const status = clutchStatus(ui, showSticks);

  return (
    <Tdm56Ctx.Provider value={api}>
      <div
        className="tdm56"
        style={{ ['--tdm56-drive' as string]: '#f0b400', ['--tdm56-stop' as string]: '#d4221a', ['--tdm56-ok' as string]: '#1f9d6b' }}
      >
        {/* ---------------- top bar: always visible */}
        <div className="sticky top-0 z-30 -mx-4 mb-4 border-b border-zinc-200 bg-zinc-50/95 px-4 py-2 backdrop-blur dark:border-zinc-800 dark:bg-zinc-950/95">
          <div className="flex items-center gap-3">
            <div
              className={`flex min-w-0 flex-1 items-center gap-3 rounded-xl px-3 py-2 transition-colors ${
                status.tone === 'drive'
                  ? 'bg-[var(--tdm56-drive)] text-black'
                  : status.tone === 'blocked'
                    ? 'bg-red-50 text-red-900 ring-1 ring-red-200 dark:bg-red-950/50 dark:text-red-100 dark:ring-red-900'
                    : 'bg-zinc-100 text-zinc-800 dark:bg-zinc-900 dark:text-zinc-200'
              }`}
              aria-live="polite"
              data-testid="tdm56-clutch-status"
              data-state={status.tone}
            >
              <span
                className={`inline-block h-3 w-3 shrink-0 rounded-full ${
                  status.tone === 'drive' ? 'animate-pulse bg-black' : status.tone === 'blocked' ? 'bg-red-600' : 'bg-zinc-400'
                }`}
              />
              <div className="min-w-0">
                <div className="truncate text-base font-semibold leading-tight sm:text-lg">{status.title}</div>
                <div className="truncate text-xs opacity-80 sm:text-sm">{status.sub}</div>
              </div>
            </div>
            {controlSlot && <div className="hidden shrink-0 md:block">{controlSlot}</div>}
            <button
              type="button"
              onClick={pressStop}
              onPointerDown={(e) => {
                // fire on press, not release: every millisecond counts
                e.preventDefault();
                pressStop();
              }}
              className="h-16 shrink-0 rounded-2xl border-4 border-red-900/30 bg-[var(--tdm56-stop)] px-6 text-2xl font-black tracking-wide text-white shadow-[0_4px_0_#7f1410] active:translate-y-1 active:shadow-none focus-visible:outline focus-visible:outline-4 focus-visible:outline-offset-2 focus-visible:outline-red-600 sm:h-20 sm:px-10 sm:text-3xl"
              aria-label="Stop the robot (Escape)"
              data-testid="tdm56-stop"
            >
              STOP
            </button>
          </div>
          {controlSlot && <div className="mt-2 md:hidden">{controlSlot}</div>}
          {(localStop || link.stopPending) && (
            <div className="mt-2 flex flex-wrap items-center gap-3 rounded-lg bg-red-600 px-3 py-2 text-sm text-white" role="alert">
              <span className="font-semibold">
                {link.stopPending
                  ? 'STOP not delivered: the control link is down. Use the physical e-stop at the table. Retrying on reconnect.'
                  : robot?.stopped
                    ? 'Robot stopped. It stays locked until someone at the arm clears it.'
                    : 'STOP sent. Motion from this page is off.'}
              </span>
              {!link.stopPending && (
                <button
                  type="button"
                  className="rounded-md bg-white px-3 py-1 font-medium text-red-700 hover:bg-red-50"
                  onClick={() => {
                    localStopRef.current = false;
                    setLocalStop(false);
                  }}
                >
                  Resume control
                </button>
              )}
            </div>
          )}
        </div>

        {dev && (
          <p className="mb-3 rounded-md bg-amber-100 px-3 py-1.5 text-sm text-amber-900 dark:bg-amber-950 dark:text-amber-200" data-testid="tdm56-dev">
            Dev link: connected to <code className="text-xs">{dev.streamUrl}</code> with a fixed token, not a claimed task. If this is the
            spikes/tdm-56 mock, the robot is simulated.
          </p>
        )}

        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_320px]">
          {/* ---------------- main view */}
          <div className="min-w-0">
            <div
              className={`rounded-xl outline-offset-4 transition-[outline-color] ${ui.engaged ? 'outline-4 outline-[var(--tdm56-drive)] outline-solid' : 'outline-0 outline-transparent'}`}
              data-testid="tdm56-feed-frame"
            >
              {dev ? (
                <CameraFeed getGrant={getGrant} onHealth={onHealth} />
              ) : (
                <TaskCameraFeed taskId={taskId} onHealth={onHealth} />
              )}
            </div>

            {/* phone / touch controls */}
            <div className="mt-3 flex items-center justify-between gap-2">
              <button
                type="button"
                className="rounded-md px-2 py-1 text-sm text-zinc-600 underline-offset-2 hover:underline dark:text-zinc-400"
                onClick={() => setShowSticks((s) => !s)}
                data-testid="tdm56-toggle-sticks"
              >
                {showSticks ? 'Hide on-screen controls' : 'Show on-screen controls'}
              </button>
              {showSticks && (
                <label className="flex items-center gap-2 text-sm text-zinc-600 dark:text-zinc-400">
                  <input type="checkbox" checked={stickEngages} onChange={(e) => setStickEngages(e.target.checked)} />
                  Touching a stick holds the clutch
                </label>
              )}
            </div>
            {showSticks && (
              <>
              {/* phones: pinned to the bottom of the screen so the sticks never scroll away */}
              <div className="h-52 sm:hidden" aria-hidden />
              <div className="fixed inset-x-0 bottom-0 z-20 border-t border-zinc-200 bg-zinc-50/95 px-3 pt-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] backdrop-blur dark:border-zinc-800 dark:bg-zinc-950/95 sm:static sm:mt-2 sm:rounded-xl sm:border sm:px-4">
                <div className="mx-auto flex max-w-md items-end justify-between gap-2">
                  <Tdm56Joystick size={stickSize} label="Move" hint="forward, back, left, right" onChange={(v) => (stickL.current = v)} />
                  <div className="flex flex-col items-center gap-2">
                    <button
                      type="button"
                      className={`h-24 w-24 touch-none select-none rounded-full border-4 text-sm font-bold transition-colors ${
                        ui.engaged
                          ? 'border-black/20 bg-[var(--tdm56-drive)] text-black'
                          : 'border-zinc-300 bg-white text-zinc-800 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-100'
                      }`}
                      onPointerDown={(e) => {
                        try {
                          e.currentTarget.setPointerCapture(e.pointerId);
                        } catch {
                          /* synthetic or already-released pointer */
                        }
                        clutchBtn.current = true;
                      }}
                      onPointerUp={() => (clutchBtn.current = false)}
                      onPointerCancel={() => (clutchBtn.current = false)}
                      onLostPointerCapture={() => (clutchBtn.current = false)}
                      onContextMenu={(e) => e.preventDefault()}
                      data-testid="tdm56-clutch-button"
                    >
                      Hold to
                      <br />
                      drive
                    </button>
                    <GripperSlider value={gripper} onChange={setGripper} compact />
                  </div>
                  <Tdm56Joystick size={stickSize} label="Lift and turn" hint="up, down, turn" onChange={(v) => (stickR.current = v)} />
                </div>
              </div>
              </>
            )}
          </div>

          {/* ---------------- side panel */}
          <aside className="flex flex-col gap-4 text-sm">
            <Panel title="Robot">
              <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5" data-testid="tdm56-robot">
                <dt className="text-zinc-500">Link</dt>
                <dd className="flex items-center gap-2">
                  <span
                    className={`inline-block h-2 w-2 rounded-full ${
                      link.state === 'open' ? 'bg-[var(--tdm56-ok)]' : link.state === 'denied' || link.state === 'error' ? 'bg-red-500' : 'bg-zinc-400'
                    }`}
                  />
                  <span data-testid="tdm56-link-state">{LINK_TEXT[link.state]}</span>
                  {link.ackRttMs != null && <span className="text-zinc-500 tabular-nums">{link.ackRttMs} ms</span>}
                </dd>
                <dt className="text-zinc-500">Sending</dt>
                <dd className="tabular-nums" data-testid="tdm56-cmd-hz">
                  {link.cmdHz} cmd/s
                </dd>
                <dt className="text-zinc-500">Driven by</dt>
                <dd data-testid="tdm56-control-source">{robot?.controlSource ?? '--'}</dd>
                <dt className="text-zinc-500">Hand at</dt>
                <dd className="tabular-nums">
                  {robot?.pos ? `${fmt(robot.pos[0], 1, 100)}, ${fmt(robot.pos[1], 1, 100)}, ${fmt(robot.pos[2], 1, 100)} cm` : '--'}
                </dd>
                <dt className="text-zinc-500">Orientation</dt>
                <dd className="tabular-nums">{robot?.rot ? fmtRot(robot.rot, robot.rotKind) : '--'}</dd>
                <dt className="text-zinc-500">Gripper</dt>
                <dd className="tabular-nums">
                  {robot?.gripper != null ? `${Math.round(robot.gripper * 100)}% open` : '--'}
                  <span className="text-zinc-500"> (asked {Math.round(ui.gripper * 100)}%)</span>
                </dd>
                <dt className="text-zinc-500">Safety</dt>
                <dd data-testid="tdm56-clamps">
                  {robot?.stopped ? (
                    <span className="rounded bg-red-600 px-1.5 py-0.5 text-xs font-semibold text-white">Latched stop</span>
                  ) : robot?.watchdog ? (
                    <span className="rounded bg-amber-500 px-1.5 py-0.5 text-xs font-semibold text-black">Watchdog hold</span>
                  ) : robot?.clamps.length ? (
                    <span className="flex flex-wrap gap-1">
                      {robot.clamps.map((c) => (
                        <span key={c} className="rounded bg-amber-100 px-1.5 py-0.5 text-xs text-amber-900 dark:bg-amber-950 dark:text-amber-200">
                          {CLAMP_TEXT[c] ?? c}
                        </span>
                      ))}
                    </span>
                  ) : robot ? (
                    <span className="text-zinc-500">No limits hit</span>
                  ) : (
                    '--'
                  )}
                </dd>
                {robot?.recording != null && (
                  <>
                    <dt className="text-zinc-500">Recording</dt>
                    <dd>{fmtRecording(robot.recording)}</dd>
                  </>
                )}
                {ui.engaged && (
                  <>
                    <dt className="text-zinc-500">This stroke</dt>
                    <dd className="tabular-nums">
                      {Math.round(Math.hypot(...ui.dPos) * 1000)} mm, {Math.round((Math.hypot(...ui.dRot) * 180) / Math.PI)}°
                    </dd>
                  </>
                )}
              </dl>
            </Panel>

            <Panel title="Speed">
              <div className="flex items-baseline justify-between">
                <span className="text-2xl font-semibold tabular-nums" data-testid="tdm56-speed">
                  {Math.round(effSpeed * 100)}%
                </span>
                <label className="flex items-center gap-2">
                  <input type="checkbox" checked={slow} onChange={(e) => setSlow(e.target.checked)} data-testid="tdm56-slow" />
                  Slow mode
                </label>
              </div>
              <input
                type="range"
                min={0.1}
                max={1}
                step={0.05}
                value={speed}
                onChange={(e) => setSpeed(Number(e.target.value))}
                className="mt-2 w-full accent-zinc-700 dark:accent-zinc-300"
                aria-label="Speed scale"
              />
              <p className="mt-1 text-xs text-zinc-500">
                {slow ? `Slow mode caps speed at ${SLOW_CAP * 100}%. Turn it off once you are comfortable.` : 'Full range. The robot still applies its own speed limits.'}
              </p>
            </Panel>

            <Panel title="Gripper">
              <GripperSlider value={gripper} onChange={setGripper} />
              <p className="mt-1 text-xs text-zinc-500">The gripper moves while you hold the clutch.</p>
            </Panel>

            {recordingSlot && <Panel title="Recording">{recordingSlot}</Panel>}

            <Panel title="Controls">
              <p className="mb-2 text-xs text-zinc-500" data-testid="tdm56-gamepad">
                {ui.pad ? `Gamepad connected: ${ui.pad}` : 'No gamepad found. Press any button on one to connect it.'}
              </p>
              <KeyTable
                rows={[
                  ['Drive (clutch)', 'Hold Space', 'Hold RT'],
                  ['Forward / back', 'W / S', 'Left stick'],
                  ['Left / right', 'A / D', 'Left stick'],
                  ['Up / down', 'Q / E', 'Right stick up/down'],
                  ['Turn', '← / →', 'Right stick left/right'],
                  ['Tilt', '↑ / ↓', 'D-pad up/down'],
                  ['Roll', 'Shift + ← / →', 'D-pad left/right'],
                  ['Gripper', 'R open, F close', 'A toggles, LB/RB'],
                  ['Stop', 'Esc', 'B'],
                ]}
              />
              <p className="mt-2 text-xs text-zinc-500">
                Moves are relative to where the hand was when you pressed the clutch. Let go to hold still.
              </p>
            </Panel>
          </aside>
        </div>
        {feed && (
          <span className="sr-only" data-testid="tdm56-feed-health">
            {JSON.stringify(feed)}
          </span>
        )}
      </div>
    </Tdm56Ctx.Provider>
  );
}

// ------------------------------------------------------------------ bits

function clutchStatus(ui: UiSnap, touch: boolean): { tone: 'drive' | 'blocked' | 'idle'; title: string; sub: string } {
  if (ui.engaged) return { tone: 'drive', title: 'Driving', sub: `Clutch held (${ui.source ?? 'input'}). Let go to hold still.` };
  if (ui.block) {
    return {
      tone: ui.wantClutch || ui.needsRelease ? 'blocked' : 'idle',
      title: ui.wantClutch ? 'Clutch released for safety' : 'Not driving',
      sub: ui.block.text,
    };
  }
  if (ui.needsRelease) return { tone: 'blocked', title: 'Let go of the clutch', sub: 'Then press it again to drive.' };
  return {
    tone: 'idle',
    title: 'Not driving',
    sub: touch ? 'Hold the drive button or touch a stick.' : 'Hold Space (or RT on a gamepad) to drive.',
  };
}

const LINK_TEXT: Record<Tdm56LinkSnapshot['state'], string> = {
  idle: 'Idle',
  connecting: 'Connecting',
  open: 'Connected',
  reconnecting: 'Reconnecting',
  denied: 'No access',
  error: 'Error',
  closed: 'Closed',
};

const CLAMP_TEXT: Record<string, string> = {
  clamped_joint_limit: 'Joint limit',
  clamped_speed: 'Speed cap',
  clamped_workspace: 'Workspace edge',
  clamped_gripper: 'Gripper range',
  rejected_jump: 'Jump rejected',
  rejected_nan: 'Bad value rejected',
  watchdog_hold: 'Watchdog hold',
};

function fmtRot(r: number[], kind?: 'quat' | 'rpy') {
  let rpy = r;
  if (kind === 'quat' && r.length === 4) {
    const [x, y, z, w] = r;
    rpy = [
      Math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y)),
      Math.asin(Math.max(-1, Math.min(1, 2 * (w * y - z * x)))),
      Math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z)),
    ];
  }
  return rpy
    .slice(0, 3)
    .map((v) => `${Math.round((v * 180) / Math.PI)}°`)
    .join(', ');
}

function fmtRecording(r: unknown): string {
  if (typeof r === 'boolean') return r ? 'On' : 'Off';
  if (typeof r === 'string') return r;
  if (r && typeof r === 'object') {
    const o = r as Record<string, unknown>;
    if (typeof o.active === 'boolean') return o.active ? 'On' : 'Off';
    if (typeof o.state === 'string') return o.state;
  }
  return JSON.stringify(r);
}

function Panel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="rounded-xl border border-zinc-200 bg-white p-3 dark:border-zinc-800 dark:bg-zinc-900">
      <h2 className="mb-2 text-sm font-semibold text-zinc-900 dark:text-zinc-100">{title}</h2>
      {children}
    </section>
  );
}

function GripperSlider({ value, onChange, compact }: { value: number; onChange: (v: number) => void; compact?: boolean }) {
  return (
    <div className={compact ? 'w-24' : 'w-full'}>
      <input
        type="range"
        min={0}
        max={1}
        step={0.01}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="w-full touch-none accent-[var(--tdm56-drive)]"
        aria-label="Gripper opening"
        data-testid={compact ? 'tdm56-gripper-compact' : 'tdm56-gripper'}
      />
      <div className="flex justify-between text-xs text-zinc-500">
        <span>Closed</span>
        {!compact && <span className="tabular-nums">{Math.round(value * 100)}%</span>}
        <span>Open</span>
      </div>
    </div>
  );
}

function KeyTable({ rows }: { rows: [string, string, string][] }) {
  return (
    <table className="w-full text-xs">
      <thead>
        <tr className="text-left text-zinc-500">
          <th className="pb-1 font-normal" />
          <th className="pb-1 font-medium">Keyboard</th>
          <th className="pb-1 font-medium">Gamepad</th>
        </tr>
      </thead>
      <tbody>
        {rows.map(([what, k, g]) => (
          <tr key={what} className="border-t border-zinc-100 dark:border-zinc-800">
            <td className="py-1 pr-2 text-zinc-600 dark:text-zinc-400">{what}</td>
            <td className="py-1 pr-2">
              <kbd className="rounded border border-zinc-300 bg-zinc-50 px-1 font-sans dark:border-zinc-700 dark:bg-zinc-800">{k}</kbd>
            </td>
            <td className="py-1">{g}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
