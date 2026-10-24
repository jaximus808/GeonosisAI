'use client';

// TDM-56: the teleop console mounted by /teleop/[taskId].
//
// Drives the arm over the TDM-55 stream server's `/control` WebSocket (lib/teleop/tdm56ControlClient.ts)
// with clutch control: motion is sent ONLY while a clutch is held, as deltas from the pose at
// clutch-down, ~30 Hz. TDM-106: the inputs live in the shared lib/teleop/useTdm106Drive.ts (same loop
// as practice): holding a move key is the clutch (Space / RT / the on-screen button / a stick still
// work), translation only unless "Turn the hand" is on, slow default speed with a smooth ramp, G for
// the gripper. The keycap legend, sticks and gripper button are always on screen.
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
//   assistTarget               TDM-106 snap-to-grasp seam: {tool, obj} in the base frame when the object pose
//                              is known (a sim robot's state.scene); without it the assist toggle is disabled

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { CameraFeed, TaskCameraFeed, type FeedHealth } from '@/components/teleop/CameraFeed';
import { TaskSimCameras } from '@/components/robotview/SimCameras';
import { Tdm106DriveSettings, Tdm106KeyLegend, Tdm106OnScreenControls } from '@/components/teleop/Tdm106Controls';
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
import type { V3 } from '@/lib/teleop/tdm56Input';
import { useTdm106Drive, type Tdm106DriveUi } from '@/lib/teleop/useTdm106Drive';

const ROBOT_SILENT_MS = 1000;

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
  /** TDM-106 snap-to-grasp: tool + object positions (base frame), when the object pose is known (sim robots). */
  assistTarget?: () => { tool: V3; obj: V3 } | null;
}

type Block = { code: string; text: string } | null;

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
  assistTarget,
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

  const canDriveRef = useRef({ canDrive, canDriveReason });
  canDriveRef.current = { canDrive, canDriveReason };
  const maxLatRef = useRef(maxLatencyMs);
  maxLatRef.current = maxLatencyMs;
  const assistRef = useRef(assistTarget);
  assistRef.current = assistTarget;

  // STOP pressed here: stays latched until the operator confirms resume.
  const [localStop, setLocalStop] = useState(false);
  const localStopRef = useRef(false);
  localStopRef.current = localStop;

  // two sticks + the middle column must fit a 390 px phone
  const [stickSize, setStickSize] = useState(112);
  const [touch, setTouch] = useState(false);
  useEffect(() => {
    setTouch(window.matchMedia?.('(pointer: coarse)').matches ?? false);
    const fit = () => setStickSize(window.innerWidth < 420 ? 100 : 112);
    fit();
    window.addEventListener('resize', fit);
    return () => window.removeEventListener('resize', fit);
  }, []);

  const pressStop = useCallback(() => {
    localStopRef.current = true;
    setLocalStop(true);
    clientRef.current?.sendStop();
  }, []);

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

  // ---- inputs: the shared TDM-106 loop (keyboard, gamepad, sticks, clutch button, gripper)
  const drive = useTdm106Drive({
    blockReason,
    pressStop,
    assistTarget: () => assistRef.current?.() ?? null,
    send: (cmd, isRelease) => {
      if (isRelease) {
        // One final clutch:false so the robot holds at once instead of waiting for its 200 ms watchdog.
        // Stamped when the lease allows it; otherwise sent bare (it can only make the arm hold).
        clientRef.current?.sendCmd(gate(cmd) ?? cmd);
        return true;
      }
      const out = gate(cmd);
      if (!out) return false; // the lease gate said no at this instant: let go, press again later
      clientRef.current?.sendCmd(out);
      return true;
    },
  });
  const ui = drive.ui;

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
  const status = clutchStatus(ui, touch);

  return (
    <Tdm56Ctx.Provider value={api}>
      <div
        className="tdm56"
        style={{ ['--tdm56-drive' as string]: 'var(--melt)', ['--tdm56-stop' as string]: '#d4221a', ['--tdm56-ok' as string]: '#1f9d6b' }}
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
                // TDM-105: a sim robot has no video; its rendered top / side / wrist views stand in (health from its state feed)
                <TaskSimCameras taskId={taskId} onHealth={onHealth} fallback={<TaskCameraFeed taskId={taskId} onHealth={onHealth} />} />
              )}
            </div>

            {/* TDM-106: the controls are always on screen: the keycap legend lights up as you press, the
                sticks / hold-to-drive / gripper button work with a mouse or a thumb */}
            <div className="mt-3 grid gap-3">
              {!touch && <Tdm106KeyLegend drive={drive} testPrefix="tdm56" />}
              <Tdm106OnScreenControls drive={drive} stickSize={stickSize} testPrefix="tdm56" />
              {touch && <Tdm106KeyLegend drive={drive} testPrefix="tdm56" />}
            </div>
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

            <Panel title="Driving">
              <Tdm106DriveSettings drive={drive} snapAvailable={!!assistTarget} testPrefix="tdm56" />
            </Panel>

            <Panel title="Gripper">
              <GripperSlider value={drive.gripper} onChange={drive.setGripper} />
              <p className="mt-1 text-xs text-zinc-500">Tap G or the grip button to open or close. The arm holds still while the gripper moves.</p>
            </Panel>

            {recordingSlot && <Panel title="Recording">{recordingSlot}</Panel>}

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

function clutchStatus(ui: Tdm106DriveUi, touch: boolean): { tone: 'drive' | 'blocked' | 'idle'; title: string; sub: string } {
  if (ui.engaged)
    return {
      tone: 'drive',
      title: 'Driving',
      sub: ui.source === 'gripper' ? 'Moving the gripper. The arm holds still.' : ui.source === 'keys' ? 'Let go of the keys to hold still.' : `Clutch held (${ui.source ?? 'input'}). Let go to hold still.`,
    };
  if (ui.block) {
    return {
      tone: ui.want || ui.needsRelease ? 'blocked' : 'idle',
      title: ui.want ? 'Paused for safety' : 'Not driving',
      sub: ui.block.text,
    };
  }
  if (ui.needsRelease) return { tone: 'blocked', title: 'Let go of the keys', sub: 'Then press them again to drive.' };
  return {
    tone: 'idle',
    title: 'Not driving',
    sub: touch ? 'Drag a stick to move the hand.' : 'Hold W A S D to move, Q / E for up and down, tap G to grip.',
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
