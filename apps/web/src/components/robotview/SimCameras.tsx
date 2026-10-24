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
import { useContributor } from '@/lib/contributor/hooks';
import { SIM_DEFAULT_SCENE, type SceneSpecV1 } from '@/lib/robotview/scene';
import { classifyRobot } from '@/lib/robotview/sources';
import type { RobotLiveState } from '@/lib/robotview/types';
import { useRobotLive, type RobotLiveResult } from '@/lib/robotview/useRobotLive';
import { useRows } from '@/lib/spacetime/hooks';

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

function Pane({ id, state, spec, primary, stale, className }: {
  id: string;
  state: RobotLiveState | null;
  spec: SceneSpecV1;
  primary: boolean;
  stale: boolean;
  className?: string;
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
          ghost={primary ? state?.chunk_preview : undefined}
          className="h-full w-full"
        />
      </div>
      {/* steel wash at the edges: reads as "not a real camera" without hiding the scene */}
      <div aria-hidden className="pointer-events-none absolute inset-0 shadow-[inset_0_0_0_1px_rgb(167_139_250/0.35),inset_0_0_36px_rgb(76_29_149/0.55)]" />
      <figcaption className="pointer-events-none absolute bottom-1.5 left-1.5 flex items-center gap-1.5 rounded bg-black/55 px-1.5 py-0.5 text-[11px] font-medium text-white/90 backdrop-blur-sm">
        <span aria-hidden className="h-2 w-2 rounded-[2px]" style={{ backgroundImage: SIM_TAPE }} />
        {name}
      </figcaption>
    </figure>
  );
}

export function SimCameras({ state, spec, cameras, stale = false, hz, waitingDetail, robotName, framed = true, className }: SimCamerasProps) {
  const scene = spec ?? SIM_DEFAULT_SCENE;
  const ids = useMemo(() => (cameras && cameras.length ? [...cameras] : [...SIM_CAMERA_IDS]), [cameras]);
  const [first, ...rest] = ids;
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
          <Pane id={first} state={state} spec={scene} primary stale={stale} />
          {framed && (
          <span
            className="gx-tag gx-tag-sim absolute left-2 top-2 shadow-[inset_0_0_0_1px_var(--steel)]"
            title="These views are drawn in your browser from the simulated robot's joint state. No real camera is involved."
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
            <Pane id={id} state={state} spec={scene} primary={false} stale={stale} />
          </div>
        ))}
      </div>
      <p className="mt-1.5 flex flex-wrap items-center gap-x-3 px-0.5 text-[11px] text-ink-muted font-mono tabular-nums">
        <span>Drawn from the sim robot&apos;s joint state, not a video feed.</span>
        <span className="ml-auto">
          {hz != null && `${hz.toFixed(0)} Hz state, `}
          {fps} fps
        </span>
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

function SimTaskPane({ taskId, robotId, robotName, onHealth }: {
  taskId: bigint;
  robotId: number;
  robotName: string;
  onHealth?: (h: FeedHealth) => void;
}) {
  const { identity } = useContributor();
  const live = useRobotLive(robotId, { taskId, identity });
  const health = simFeedHealth(live);
  const key = `${health.state}|${health.stale}|${health.worstLatencyMs == null ? '' : Math.round(health.worstLatencyMs / 25)}`;
  const onHealthRef = useRef(onHealth);
  onHealthRef.current = onHealth;
  useEffect(() => {
    onHealthRef.current?.(health);
  }, [key]); // eslint-disable-line react-hooks/exhaustive-deps
  return <SimCameras state={live.state} stale={live.stale} hz={live.hz} waitingDetail={live.error} robotName={robotName} />;
}

/**
 * The teleop video pane: for a simulated robot, the three rendered views (no video stream needed,
 * and its health comes from the state feed); for any other robot, `fallback` (the real camera feed).
 */
export function TaskSimCameras({ taskId, onHealth, fallback }: {
  taskId: bigint | number | string;
  onHealth?: (h: FeedHealth) => void;
  fallback: ReactNode;
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
    return <div className="grid aspect-[4/3] place-items-center rounded-lg bg-zinc-900 text-sm text-zinc-400">Finding this task&apos;s robot…</div>;
  }
  if (!robot || classifyRobot(robot).kind !== 'sim') return <>{fallback}</>;
  return <SimTaskPane taskId={id} robotId={Number(robot.id)} robotName={robot.name} onHealth={onHealth} />;
}

export default SimCameras;

/**
 * Ready-made filler for RobotView's `simCameras` slot: reuses RobotView's own state feed (no second socket).
 *   <RobotView simCameras={simCamerasSlot} />
 */
export function simCamerasSlot({ live, compact }: { live: RobotLiveResult; robotId: number; compact: boolean }): ReactNode {
  return <SimCameras state={live.state} stale={live.stale} hz={live.hz} waitingDetail={live.error} framed={false} cameras={compact ? ['top', 'wrist'] : undefined} />;
}
