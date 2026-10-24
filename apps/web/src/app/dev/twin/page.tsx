'use client';

// TDM-97 harness for <RobotTwin3D> (not linked from the nav; RobotView mounts the twin on real pages).
//   /dev/twin                         scripted pick-and-place in YAM joint space (FIXTURE, no robot)
//   /dev/twin?src=fixture             TDM-96's recorded sim state (SimArm kinematics, not the YAM's)
//   /dev/twin?q=0,0.98,0.93,-0.73,0,0 hold one pose (the FK / screenshot-pair check)
//   /dev/twin?ws=<ws url>&token=<t>   a real /ws/state feed (e.g. the MuJoCo sim robot)
//   &preset=front|top|wrist  &sim=0  &ghost=0  &w=640 (frame width in px, 4:3)
// In dev, window.__rdfTwin.ee() returns the rendered grasp_site position (robot frame, m).

import dynamic from 'next/dynamic';
import { useEffect, useMemo, useRef, useState } from 'react';
import type { RobotLiveState, Vec3 } from '@/lib/robotview/types';
import { useRobotLive } from '@/lib/robotview/useRobotLive';
import { createYamKinematics, YAM_READY } from '@/components/robotview/yamFk';
import { YAM_MODEL } from '@/components/robotview/yamModel';
import type { TwinPreset } from '@/components/robotview/RobotTwin3D';

const RobotTwin3D = dynamic(() => import('@/components/robotview/RobotTwin3D').then((m) => m.RobotTwin3D), { ssr: false });
const KIN = createYamKinematics(YAM_MODEL);

// Joint waypoints solved with yam_kinematics IK (PoE): tool pitched down over the cube / plate of the
// MuJoCo scene. [q(6), gripper, seconds to get there, carrying cube]
type Wp = [number[], number, number, boolean];
const PLAN: Wp[] = [
  [YAM_READY, 1, 1.4, false],
  [[-0.424, 1.421, 1.084, -0.792, 0, 0], 1, 1.6, false], // over the cube
  [[-0.424, 1.527, 0.803, -0.406, 0, 0], 1, 1.0, false], // at the cube
  [[-0.424, 1.527, 0.803, -0.406, 0, 0], 0.42, 0.6, false], // close
  [[-0.424, 1.374, 1.181, -0.887, 0, 0], 0.42, 1.0, true], // lift
  [[0.55, 1.463, 1.216, -0.833, 0, 0], 0.42, 1.8, true], // over the plate
  [[0.55, 1.559, 0.985, -0.557, 0, 0], 0.42, 0.9, true], // lower
  [[0.55, 1.559, 0.985, -0.557, 0, 0], 1, 0.6, false], // release
  [[0.55, 1.463, 1.216, -0.833, 0, 0], 1, 0.9, false],
  [YAM_READY, 1, 1.6, false],
];
const CUBE_HOME: Vec3 = [0.31, -0.14, 0.02];
const PLATE: Vec3 = [0.31, 0.19, 0];
const WORKSPACE = { min: [0.1, -0.35, 0.0] as Vec3, max: [0.5, 0.35, 0.45] as Vec3, table_z: 0 }; // safety.yaml, yam profile

const ease = (t: number) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
const lerp = (a: number[], b: number[], t: number) => a.map((v, i) => v + (b[i] - v) * t);
const TOTAL = PLAN.reduce((s, w) => s + w[2], 0);

function scriptedState(tSec: number, withGhost: boolean): RobotLiveState {
  let t = tSec % TOTAL;
  let i = 0;
  while (t > PLAN[i][2]) t -= PLAN[i++][2];
  const prev = PLAN[(i + PLAN.length - 1) % PLAN.length];
  const cur = PLAN[i];
  const u = ease(Math.min(1, t / cur[2]));
  const q = lerp(prev[0], cur[0], u);
  const gripper = prev[1] + (cur[1] - prev[1]) * u;
  const ee = KIN.graspSitePos(q);
  const placed = i >= 7 || i === 0;
  const carrying = cur[3] || (i === 7 && u < 0.3);
  const cube: Vec3 = carrying ? [ee[0], ee[1], ee[2] - 0.004] : placed && i !== 0 ? [PLATE[0], PLATE[1], 0.032] : CUBE_HOME;
  // the next ~1.6 s of the plan, downsampled: what a policy chunk preview looks like
  const ghost: number[][] = [];
  if (withGhost) {
    for (let k = 1; k <= 10; k++) {
      let tt = (tSec + k * 0.16) % TOTAL;
      let j = 0;
      while (tt > PLAN[j][2]) tt -= PLAN[j++][2];
      ghost.push(lerp(PLAN[(j + PLAN.length - 1) % PLAN.length][0], PLAN[j][0], ease(Math.min(1, tt / PLAN[j][2]))));
    }
  }
  return {
    t_ms: Date.now(),
    joints: q,
    gripper,
    ee: { pos: ee, quat: [1, 0, 0, 0] },
    control_source: 'policy',
    robot_state: 'fixture',
    estop: false,
    safety: { clamps_1s: {} },
    scene: { cube: { pos: cube, quat: [1, 0, 0, 0] }, plate: { pos: PLATE } },
    chunk_preview: withGhost ? ghost : undefined,
    workspace: WORKSPACE,
  };
}

interface Opts {
  src: 'script' | 'fixture' | 'pose' | 'ws';
  q: number[] | null;
  preset: TwinPreset;
  sim: boolean;
  ghost: boolean;
  width: number | null;
  ws?: string;
  token?: string;
}

export default function TwinDevPage() {
  const [o, setO] = useState<Opts | null>(null);
  useEffect(() => {
    const p = new URLSearchParams(window.location.search);
    const q = p.get('q')?.split(',').map(Number) ?? null;
    const preset = p.get('preset');
    const ws = p.get('ws') ?? undefined;
    setO({
      src: ws ? 'ws' : q ? 'pose' : p.get('src') === 'fixture' ? 'fixture' : 'script',
      q: q && q.length >= 6 && q.every(Number.isFinite) ? q : null,
      preset: preset === 'top' || preset === 'wrist' ? preset : 'front',
      sim: p.get('sim') !== '0',
      ghost: p.get('ghost') !== '0',
      width: Number(p.get('w')) > 0 ? Number(p.get('w')) : null,
      ws,
      token: p.get('token') ?? undefined,
    });
  }, []);
  if (!o) return null;
  return <Harness o={o} />;
}

function Harness({ o }: { o: Opts }) {
  const [preset, setPreset] = useState<TwinPreset>(o.preset);
  const live = useRobotLive(null, { enabled: o.src === 'ws' || o.src === 'fixture', fixture: o.src === 'fixture', url: o.ws, token: o.token });
  const [scripted, setScripted] = useState<RobotLiveState | null>(null);
  const t0 = useRef(performance.now());
  useEffect(() => {
    if (o.src !== 'script' && o.src !== 'pose') return;
    if (o.src === 'pose') {
      const q = o.q!;
      setScripted({ ...scriptedState(0, false), joints: q.slice(0, 6), gripper: q[6] ?? 1, ee: { pos: KIN.graspSitePos(q), quat: [1, 0, 0, 0] }, scene: { cube: { pos: CUBE_HOME, quat: [1, 0, 0, 0] }, plate: { pos: PLATE } } });
      return;
    }
    const id = setInterval(() => setScripted(scriptedState((performance.now() - t0.current) / 1000, o.ghost)), 1000 / 30);
    return () => clearInterval(id);
  }, [o]);
  const state = o.src === 'script' || o.src === 'pose' ? scripted : live.state;
  const fps = useFps();
  const ee = useMemo(() => (state ? KIN.graspSitePos(state.joints) : null), [state]);

  const note =
    o.src === 'script'
      ? 'FIXTURE: scripted pick-and-place in YAM joint space. No robot, no policy.'
      : o.src === 'fixture'
        ? 'FIXTURE / SIMULATED: TDM-96 recording of the SimArm kinematic sim (not the YAM kinematics, so some joints sit outside the YAM ranges).'
        : o.src === 'pose'
          ? 'Static pose from ?q= (FK check).'
          : `Live /ws/state feed: ${live.transport}${live.error ? `, ${live.error}` : ''}`;

  return (
    <>
      <h1 className="mb-1 text-2xl font-semibold tracking-tight">YAM digital twin</h1>
      <p className="mb-4 max-w-prose text-sm text-zinc-500">{note}</p>
      <div className="mb-3 flex flex-wrap items-center gap-2 text-sm">
        {(['front', 'top', 'wrist'] as const).map((p) => (
          <button
            key={p}
            type="button"
            aria-pressed={preset === p}
            onClick={() => setPreset(p)}
            className={`rounded-md px-3 py-1 capitalize ring-1 ring-zinc-300 dark:ring-zinc-700 ${preset === p ? 'bg-zinc-900 text-white dark:bg-white dark:text-zinc-900' : ''}`}
          >
            {p}
          </button>
        ))}
        <span className="ml-auto tabular-nums text-zinc-500" data-testid="twin-fps">
          {fps} fps{ee ? `, tool (${ee.map((v) => v.toFixed(3)).join(', ')}) m` : ''}
        </span>
      </div>
      <div
        className={`relative aspect-[4/3] overflow-hidden rounded-xl bg-zinc-950 ring-1 ring-zinc-800 ${o.width ? '' : 'w-full max-w-5xl'}`}
        style={o.width ? { width: o.width, borderRadius: 0 } : undefined}
        data-testid="twin-frame"
      >
        <RobotTwin3D state={state} preset={preset} sim={o.sim} ghost={o.ghost ? state?.chunk_preview : undefined} />
      </div>
    </>
  );
}

function useFps() {
  const [fps, setFps] = useState(0);
  useEffect(() => {
    let n = 0;
    let raf = 0;
    let last = performance.now();
    const tick = () => {
      n++;
      const now = performance.now();
      if (now - last >= 1000) {
        setFps(Math.round((n * 1000) / (now - last)));
        n = 0;
        last = now;
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, []);
  return fps;
}
