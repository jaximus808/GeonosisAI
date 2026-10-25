'use client';

// TDM-105: the sim robot's "cameras", rendered in the browser.
//
// A simulated robot has no video to stream, so its camera panes are three.js views of the deployed
// scene: the scene spec's `top` camera (mounted above the bench, looking down at the work area), a
// `side` camera, and the `wrist` camera riding the D405 mount on the flange. Every pane is a
// RobotTwin3D (TDM-97) with a fixed camera, all driven by the same RobotLiveState, so moving the arm
// moves the YAM and the scene objects in all three at once.
//
//   <SimCameras state={live.state} stale={live.stale} hz={live.hz} />     presentational (RobotView's simCameras slot)
//   <TaskSimCameras taskId onHealth fallback={<TaskCameraFeed .../>} />   teleop video pane: sim robot -> these views,
//                                                                           anything else -> the fallback (real video)
//
// Rules kept here: sim is always labelled SIMULATED with the steel frame (TDM-109 brand); nothing is drawn as live;
// with no state yet the arm stays at rest under an explicit "waiting" note (never invented motion).

import dynamic from 'next/dynamic';
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import type { FeedHealth } from '@/components/teleop/CameraFeed';
import { Tdm86Pip } from '@/components/teleop/Tdm86Phone';
import { PausedView } from '@/components/teleop/TeleopOverlay';
import { useContributor } from '@/lib/contributor/hooks';
import { SIM_DEFAULT_SCENE, type SceneSpecV1 } from '@/lib/robotview/scene';
import { classifyRobot } from '@/lib/robotview/sources';
import type { RobotLiveState } from '@/lib/robotview/types';
import { useRobotLive, type RobotLiveResult } from '@/lib/robotview/useRobotLive';
import { useRobotScene } from '@/lib/robotview/useRobotScene';
import { useRows } from '@/lib/spacetime/hooks';
import { paneLayout, twinPerf, type TwinPerf, type ViewQuality } from '@/lib/teleop/viewQuality';
import { useViewQuality } from '@/lib/teleop/useViewQuality';
import { ViewQualityToggle } from '@/components/teleop/TeleopOverlay';

const RobotTwin3D = dynamic(() => import('./RobotTwin3D').then((m) => m.RobotTwin3D), {
  ssr: false,
  loading: () => <div className="h-full w-full animate-pulse bg-steel-soft" />,
});

/** The panes, in order: the first is the large one. Ids resolve against the spec's cameras first. */
export const SIM_CAMERA_IDS = ['top', 'side', 'wrist'] as const;

const CAMERA_NAMES: Record<string, string> = { top: 'Top', side: 'Side', front: 'Front', wrist: 'Wrist' };
const cameraName = (id: string) => CAMERA_NAMES[id] ?? id.replace(/[_-]+/g, ' ').replace(/^\w/, (c) => c.toUpperCase());

const SIM_TAPE = 'repeating-linear-gradient(135deg, var(--steel) 0 8px, var(--steel-soft) 8px 16px)';

export interface SimCamerasProps {
  state: RobotLiveState | null;
  /** Scene to draw (TDM-104's per-ticket spec). Default: the MuJoCo sim's built-in scene. */
  spec?: SceneSpecV1 | null;
  /** Camera ids to show; the first is the large pane. Default top, side, wrist. */
  cameras?: readonly string[];
  /** The state feed has gone quiet (useRobotLive().stale). */
  stale?: boolean;
  /** Measured state rate (useRobotLive().hz), shown under the panes. */
  hz?: number;
  /** Why there is no state (useRobotLive().error), shown while waiting. */
  waitingDetail?: string | null;
  robotName?: string;
  /** false inside a frame that already carries the SIMULATED badge and steel tape (RobotView). Default true. */
  framed?: boolean;
  className?: string;
  /** TDM-86 phone cockpit: show only this camera, filling the parent's height (switch it with a tab bar in `overlay`). */
  single?: string;
  /** TDM-86: ghost for the large pane instead of the policy's chunk preview (null = none). */
  ghost?: number[][] | null;
  /** TDM-86: drawn over the large pane (view tabs, gripper and height readouts). */
  overlay?: ReactNode;
  /** TDM-86: a small second view in the corner of the single view (e.g. Side, to judge the hand's height). */
  pip?: { camera: string; label: string; onTap?: () => void } | null;
  /** Teleop "Quality" (lib/teleop/viewQuality.ts). Omit: every pane at full quality (Sharp). */
  quality?: ViewQuality;
  /** The user holds control: in Smooth, only the big pane and one small pane draw (the others pause). */
  driving?: boolean;
  /** Drawn at the right of the footer line (e.g. the Quality toggle). */
  toolbar?: ReactNode;
}

/** Frames per second of the page's render loop (every canvas here draws on it), over the last second. */
function useRenderFps(): number {
  const [fps, setFps] = useState(0);
  useEffect(() => {
    let raf = 0;
    let frames = 0;
    let t0 = performance.now();
    const tick = (now: number) => {
      frames++;
      if (now - t0 >= 1000) {
        setFps(Math.round((frames * 1000) / (now - t0)));
        frames = 0;
        t0 = now;
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, []);
  return fps;
}

declare global {
  interface Window {
    __rdfSimCams?: { fps: () => number; cameras: string[] };
  }
}

function Pane({ id, state, spec, primary, stale, className, ghost, caption = true, perf }: {
  id: string;
  state: RobotLiveState | null;
  spec: SceneSpecV1;
  primary: boolean;
  stale: boolean;
  className?: string;
  /** TDM-86: the ghost to draw instead of the policy's chunk preview (null = none). */
  ghost?: number[][] | null;
  caption?: boolean;
  perf?: TwinPerf | null;
}) {
  const name = cameraName(id);
  return (
    <figure
      className={`absolute inset-0 m-0 overflow-hidden rounded-md bg-surface-sunken ring-1 ring-steel/40 ${className ?? ''}`}
      data-sim-camera={id}
      aria-label={`${name} camera, simulated`}
    >
      <div className={`absolute inset-0 transition-opacity ${stale ? 'opacity-55' : ''}`}>
        <RobotTwin3D
          state={state}
          camera={id}
          spec={spec}
          sim
          controls={false}
          ghost={ghost !== undefined ? (ghost ?? undefined) : primary ? state?.chunk_preview : undefined}
          className="h-full w-full"
          perf={perf}
        />
      </div>
      {/* steel wash at the edges: reads as "not a real camera" without hiding the scene */}
      <div aria-hidden className="pointer-events-none absolute inset-0 shadow-[inset_0_0_0_1px_rgb(167_139_250/0.35),inset_0_0_36px_rgb(76_29_149/0.55)]" />
      {caption && <figcaption className="pointer-events-none absolute bottom-1.5 left-1.5 flex items-center gap-1.5 rounded bg-black/55 px-1.5 py-0.5 text-[11px] font-medium text-white/90 backdrop-blur-sm">
        <span aria-hidden className="h-2 w-2 rounded-[2px]" style={{ backgroundImage: SIM_TAPE }} />
        {name}
      </figcaption>}
    </figure>
  );
}

export function SimCameras({ state, spec, cameras, stale = false, hz, waitingDetail, robotName, framed = true, className, single, ghost, overlay, pip, quality, driving = false, toolbar }: SimCamerasProps) {
  const scene = spec ?? SIM_DEFAULT_SCENE;
  const ids = useMemo(() => (cameras && cameras.length ? [...cameras] : [...SIM_CAMERA_IDS]), [cameras]);
  const [first, ...rest] = ids;
  // Smooth while driving: the big pane + one live small pane; the others are unmounted (WebGL context freed)
  const [inset, setInset] = useState<string | null>(null);
  const layout = paneLayout(ids, { quality, driving, inset });
  const fps = useRenderFps();
  const fpsRef = useRef(fps);
  fpsRef.current = fps;
  useEffect(() => {
    if (process.env.NODE_ENV === 'production') return;
    const probe = { fps: () => fpsRef.current, cameras: ids };
    window.__rdfSimCams = probe;
    return () => {
      if (window.__rdfSimCams === probe) delete window.__rdfSimCams;
    };
  }, [ids]);

  const waiting = !state;
  if (single) {
    return (
      <section
        aria-label="Simulated camera view"
        className={`gx-foundry relative h-full overflow-hidden rounded-md bg-surface-sunken ring-2 ring-steel ${className ?? ''}`}
        data-sim-cameras={single}
        data-fps={fps}
        data-state={waiting ? 'waiting' : stale ? 'stale' : 'live'}
      >
        <Pane id={single} state={state} spec={scene} primary stale={stale} ghost={ghost} caption={false} />
        <span
          className="gx-tag gx-tag-sim absolute left-2 top-12 z-10 shadow-[inset_0_0_0_1px_var(--steel)]"
          title="Sim camera"
        >
          SIMULATED
        </span>
        {overlay}
        {pip && (
          <Tdm86Pip label={pip.label} onTap={pip.onTap}>
            <Pane id={pip.camera} state={state} spec={scene} primary={false} stale={stale} ghost={ghost} caption={false} />
          </Tdm86Pip>
        )}
        {waiting && (
          <div className="absolute inset-0 z-20 grid place-items-center bg-black/55 p-4 text-center">
            <div className="max-w-xs text-sm text-ink">
              <p className="font-semibold">Waiting for the sim robot&apos;s joint state</p>
              {waitingDetail && <p className="mt-1 text-xs text-ink-muted">{waitingDetail}</p>}
            </div>
          </div>
        )}
        {!waiting && stale && <span className="absolute right-2 top-12 z-10 gx-tag gx-tag-failed">State stalled</span>}
      </section>
    );
  }
  return (
    <section
      aria-label="Simulated camera views"
      className={`gx-foundry @container relative rounded-md bg-surface-sunken ${framed ? 'p-1.5 ring-2 ring-steel' : ''} ${className ?? ''}`}
      data-sim-cameras={ids.join(',')}
      data-fps={fps}
      data-state={waiting ? 'waiting' : stale ? 'stale' : 'live'}
    >
      {framed && <div aria-hidden className="absolute inset-x-0 top-0 h-1 rounded-t-lg" style={{ backgroundImage: SIM_TAPE }} />}
      <div className={`${framed ? 'mt-1' : ''} grid grid-cols-2 gap-1.5 @2xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)] @2xl:grid-rows-2`}>
        <div className="relative col-span-2 aspect-[4/3] @2xl:col-span-1 @2xl:row-span-2">
          <Pane id={first} state={state} spec={scene} primary stale={stale} perf={twinPerf(quality, 'main')} />
          {framed && (
          <span
            className="gx-tag gx-tag-sim absolute left-2 top-2 shadow-[inset_0_0_0_1px_var(--steel)]"
            title="Sim cameras"
          >
            SIMULATED
            {robotName && <span className="font-medium normal-case tracking-normal opacity-90">{robotName}</span>}
          </span>
          )}
          {waiting && (
            <div className="absolute inset-0 grid place-items-center bg-black/55 p-4 text-center">
              <div className="max-w-xs text-sm text-ink">
                <p className="font-semibold">Waiting for the sim robot&apos;s joint state</p>
                <p className="mt-1 text-xs text-ink-muted">The arm is shown at rest until the first sample arrives.</p>
                {waitingDetail && <p className="mt-1 text-xs text-zinc-400">{waitingDetail}</p>}
              </div>
            </div>
          )}
          {!waiting && stale && (
            <span className="absolute bottom-2 right-2 gx-tag gx-tag-failed">State stalled</span>
          )}
        </div>
        {rest.map((id) => (
          <div key={id} className="relative aspect-[4/3] @2xl:aspect-auto">
            {layout.paused.includes(id) ? (
              <PausedView name={cameraName(id)} onShow={() => setInset(id)} testId={`simcam-paused-${id}`} />
            ) : (
              <Pane id={id} state={state} spec={scene} primary={false} stale={stale} perf={twinPerf(quality, 'inset')} />
            )}
          </div>
        ))}
      </div>
      {/* one line, always: it sits between the views and the control dock */}
      <p className="mt-1.5 flex h-5 flex-nowrap items-center gap-x-3 overflow-hidden whitespace-nowrap px-0.5 text-[11px] text-ink-muted font-mono tabular-nums">
        <span className="ml-auto shrink-0">
          {hz != null && `${hz.toFixed(0)} Hz state, `}
          {fps} fps
        </span>
        {toolbar}
      </p>
    </section>
  );
}

// ---------------------------------------------------------------------------------------------
// Teleop: the video pane for a task's robot.

/** Camera-feed health derived from the state feed, for the teleop console's drive gate. */
export function simFeedHealth(live: Pick<RobotLiveResult, 'state' | 'stale' | 'latencyMs' | 'error'>): FeedHealth {
  const fresh = !!live.state && !live.stale;
  return {
    state: fresh ? 'live' : live.state ? 'reconnecting' : 'connecting',
    stale: !fresh,
    worstLatencyMs: live.latencyMs,
    detail: fresh ? 'sim views from the state feed' : (live.error ?? 'waiting for sim state'),
  };
}

/** TDM-86: what the phone cockpit passes through to the sim views. */
export interface Tdm86SimViewOpts {
  pip?: { camera: string; label: string; onTap?: () => void } | null;
  single?: string;
  spec?: SceneSpecV1 | null;
  ghost?: number[][] | null;
  overlay?: ReactNode;
  /** Every state sample (keep it cheap: store it in a ref). */
  onState?: (s: RobotLiveState | null) => void;
}

/** Desktop teleop: the Quality choice, whether the user holds control, and the footer toolbar. */
export interface DeskSimViewOpts {
  quality?: ViewQuality;
  driving?: boolean;
  toolbar?: ReactNode;
}

function SimTaskPane({ taskId, robotId, robotName, onHealth, phone, desk }: {
  taskId: bigint;
  robotId: number;
  robotName: string;
  onHealth?: (h: FeedHealth) => void;
  phone?: Tdm86SimViewOpts;
  desk?: DeskSimViewOpts;
}) {
  const { identity } = useContributor();
  const live = useRobotLive(robotId, { taskId, identity });
  // TDM-114: the scene the robot has LOADED, the same one RobotView's twin draws (else the built-in default)
  const robotScene = useRobotScene(robotId);
  return <SimLivePane live={live} robotName={robotName} onHealth={onHealth} phone={phone} desk={desk} spec={robotScene.spec} />;
}

/** The robot's loaded scene, keeping any extra cameras the caller added (e.g. the phone's driver view). */
function withCameras(robotSpec: SceneSpecV1 | null | undefined, callerSpec: SceneSpecV1 | null | undefined): SceneSpecV1 | null | undefined {
  if (!robotSpec) return callerSpec;
  const have = new Set((robotSpec.cameras ?? []).map((c) => c.id));
  const extra = (callerSpec?.cameras ?? []).filter((c) => !have.has(c.id) && !(SIM_DEFAULT_SCENE.cameras ?? []).some((d) => d.id === c.id));
  return extra.length ? { ...robotSpec, cameras: [...(robotSpec.cameras ?? []), ...extra] } : robotSpec;
}

/** Dev only: the teleop pane on the recorded sim fixture (no robot, no stream). /teleop/<id>?dev_url=fixture */
export function FixtureSimCameras({ onHealth, phone, desk }: { onHealth?: (h: FeedHealth) => void; phone?: Tdm86SimViewOpts; desk?: DeskSimViewOpts }) {
  const live = useRobotLive(null, { fixture: true });
  return <SimLivePane live={live} robotName="fixture" onHealth={onHealth} phone={phone} desk={desk} />;
}

function SimLivePane({ live, robotName, onHealth, phone, desk, spec }: {
  live: RobotLiveResult;
  /** The robot's loaded scene (null: the built-in default). */
  spec?: SceneSpecV1 | null;
  robotName: string;
  onHealth?: (h: FeedHealth) => void;
  phone?: Tdm86SimViewOpts;
  desk?: DeskSimViewOpts;
}) {
  const health = simFeedHealth(live);
  const key = `${health.state}|${health.stale}|${health.worstLatencyMs == null ? '' : Math.round(health.worstLatencyMs / 25)}`;
  const onHealthRef = useRef(onHealth);
  onHealthRef.current = onHealth;
  useEffect(() => {
    onHealthRef.current?.(health);
  }, [key]); // eslint-disable-line react-hooks/exhaustive-deps
  const onStateRef = useRef(phone?.onState);
  onStateRef.current = phone?.onState;
  useEffect(() => {
    onStateRef.current?.(live.state);
  }, [live.state]);
  if (phone?.single) {
    return (
      <SimCameras
        state={live.state}
        stale={live.stale}
        waitingDetail={live.error}
        robotName={robotName}
        single={phone.single}
        spec={withCameras(spec, phone.spec)}
        ghost={phone.ghost}
        overlay={phone.overlay}
        pip={phone.pip}
      />
    );
  }
  return (
    <SimCameras
      state={live.state}
      stale={live.stale}
      hz={live.hz}
      waitingDetail={live.error}
      robotName={robotName}
      spec={spec}
      quality={desk?.quality}
      driving={desk?.driving}
      toolbar={desk?.toolbar}
    />
  );
}

/**
 * The teleop video pane: for a simulated robot, the three rendered views (no video stream needed,
 * and its health comes from the state feed); for any other robot, `fallback` (the real camera feed).
 */
export function TaskSimCameras({ taskId, onHealth, fallback, phone, desk }: {
  taskId: bigint | number | string;
  onHealth?: (h: FeedHealth) => void;
  fallback: ReactNode;
  /** TDM-86 phone cockpit: one big view (see Tdm86SimViewOpts). */
  phone?: Tdm86SimViewOpts;
  /** Desktop: Quality / holds-control / toolbar (see DeskSimViewOpts). */
  desk?: DeskSimViewOpts;
}) {
  const id = useMemo(() => {
    try {
      return BigInt(String(taskId));
    } catch {
      return null;
    }
  }, [taskId]);
  const [tasks, tasksReady] = useRows('task', (r) => r.id.eq(id ?? 0n));
  const robotId = tasks[0]?.robotId ?? 0n;
  const [robots, robotsReady] = useRows('robot', (r) => r.id.eq(robotId));
  const robot = robots[0];

  if (id == null) return <>{fallback}</>;
  if (!tasksReady || (tasks[0] && !robotsReady)) {
    return <div className={`grid place-items-center rounded-lg bg-zinc-900 text-sm text-zinc-400 ${phone?.single ? 'h-full' : 'aspect-[4/3]'}`}>Finding this task&apos;s robot…</div>;
  }
  if (!robot || classifyRobot(robot).kind !== 'sim') return <>{fallback}</>;
  return <SimTaskPane taskId={id} robotId={Number(robot.id)} robotName={robot.name} onHealth={onHealth} phone={phone} desk={desk} />;
}

export default SimCameras;

/**
 * Ready-made filler for RobotView's `simCameras` slot: reuses RobotView's own state feed (no second socket).
 *   <RobotView simCameras={simCamerasSlot} />
 */
export function simCamerasSlot({ live, robotId, compact }: { live: RobotLiveResult; robotId: number; compact: boolean }): ReactNode {
  return <SlotSimCameras live={live} robotId={robotId} compact={compact} />;
}

/**
 * TDM-114: the slot's panes draw the scene the robot has loaded, like RobotView's twin next to them.
 * Quality (lib/teleop/viewQuality.ts): RobotView's twin is the main view, so in Smooth the slot draws ONE small
 * camera (1x, no shadows, 30 fps) with a switcher; Sharp draws all of them as before.
 */
function SlotSimCameras({ live, robotId, compact }: { live: RobotLiveResult; robotId: number; compact: boolean }) {
  const robotScene = useRobotScene(robotId);
  const [quality, setQuality] = useViewQuality();
  const toolbar = <ViewQualityToggle value={quality} onChange={setQuality} testPrefix="simslot" />;
  if (quality === 'smooth') return <SimInset state={live.state} stale={live.stale} hz={live.hz} spec={robotScene.spec} toolbar={toolbar} />;
  return (
    <SimCameras
      state={live.state}
      stale={live.stale}
      hz={live.hz}
      waitingDetail={live.error}
      framed={false}
      spec={robotScene.spec}
      cameras={compact ? ['top', 'wrist'] : undefined}
      quality="sharp"
      toolbar={toolbar}
    />
  );
}

/** One small sim camera with a Top / Side / Wrist switcher (Smooth mode next to a main twin). */
export function SimInset({ state, stale = false, hz, spec, toolbar, cameras = SIM_CAMERA_IDS, initial = 'side' }: {
  state: RobotLiveState | null;
  stale?: boolean;
  hz?: number;
  spec?: SceneSpecV1 | null;
  toolbar?: ReactNode;
  cameras?: readonly string[];
  initial?: string;
}) {
  const [cam, setCam] = useState(initial);
  const fps = useRenderFps();
  return (
    <section aria-label="Simulated camera view" className="gx-foundry relative rounded-md bg-surface-sunken" data-sim-cameras={cam} data-fps={fps} data-state={!state ? 'waiting' : stale ? 'stale' : 'live'}>
      <div className="relative aspect-[4/3]">
        <Pane id={cam} state={state} spec={spec ?? SIM_DEFAULT_SCENE} primary={false} stale={stale} perf={twinPerf('smooth', 'inset')} />
        <div className="absolute right-1.5 top-1.5 z-10 flex gap-1" role="group" aria-label="Sim camera">
          {cameras.map((id) => (
            <button
              key={id}
              type="button"
              onClick={() => setCam(id)}
              aria-pressed={cam === id}
              className={`rounded-sm px-1.5 py-0.5 text-[11px] font-semibold backdrop-blur-sm ${cam === id ? 'bg-ink text-ground' : 'bg-black/55 text-white'}`}
              data-testid={`siminset-${id}`}
            >
              {cameraName(id)}
            </button>
          ))}
        </div>
        {!state && <div className="absolute inset-0 grid place-items-center bg-black/55 text-sm text-ink">Waiting for the sim robot&apos;s joint state</div>}
      </div>
      <p className="mt-1.5 flex h-5 flex-nowrap items-center gap-x-3 overflow-hidden whitespace-nowrap px-0.5 font-mono text-[11px] tabular-nums text-ink-muted">
        <span className="min-w-0 truncate">One view drawn (Smooth). Sharp shows every camera.</span>
        <span className="ml-auto shrink-0">
          {hz != null && `${hz.toFixed(0)} Hz state, `}
          {fps} fps
        </span>
        {toolbar}
      </p>
    </section>
  );
}
