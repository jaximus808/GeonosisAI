'use client';

// TDM-59 practice mode, reworked by TDM-106 after a first-time practice run "was hard".
//
// What it drives: a client-side kinematic copy of the sim robot and the ticket's scene
// (lib/teleop/tdm106SceneSim.ts), rendered with the same 3D twin as the robot view (TDM-97). It
// takes exactly the TDM-56 contract commands, from the same input loop as the teleop console
// (lib/teleop/useTdm106Drive.ts). No robot link, no claim or lease, no recording: nothing leaves
// this page except the small network test request to /api/tdm59/ping.
//
// PRACTICE_ON_SIM_ROBOT (seam): the orchestrator's [SIM] practice sessions
// (practice_api.py, /api/practice/robots/<id>/start|token|stop|reset_scene) drive the MuJoCo sim
// robot itself over /ws/control with no lease or recording. This page uses the client-side copy
// instead, so practice works while the sim robot is busy with trials or someone else's teleop. To
// drive the sim robot here, swap `send` below for a Tdm56ControlClient on the practice token and
// feed the twin from useRobotLive(robotId); the controls and coach stay as they are.
//
// First-time flow: the controls are on screen from the start (keycap legend that lights up, sticks,
// gripper button), a coach line always names the next key, four short basics (move, up/down,
// gripper, STOP), then the task with step-by-step hints. "Skip to the task" appears once the basics
// pass. Passing the basics + the task + the network test unlocks claiming arm-control tasks
// (lib/teleop/tdm59Readiness.ts, <Tdm59ClaimGate>).

import dynamic from 'next/dynamic';
import Link from 'next/link';
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { SimTag } from '@/components/brand';
import { InstructionsOverlay } from '@/components/teleop/InstructionsOverlay';
import { LatencyBadge } from '@/components/teleop/LatencyBadge';
import { Tdm106DriveSettings, Tdm106KeyLegend, Tdm106OnScreenControls } from '@/components/teleop/Tdm106Controls';
import { YAM_READY, createYamKinematics } from '@/components/robotview/yamFk';
import { YAM_BODIES, YAM_MODEL } from '@/components/robotview/yamModel';
import type { SceneSpecV1 } from '@/lib/robotview/scene';
import type { RobotLiveState } from '@/lib/robotview/types';
import type { V3 } from '@/lib/teleop/tdm56Input';
import { tdm106Guide, type Tdm106Guide } from '@/lib/teleop/tdm106Guide';
import { createTdm106Ik } from '@/lib/teleop/tdm106Ik';
import { tdm106PracticeScene, type Tdm106SceneFetch } from '@/lib/teleop/tdm106PracticeScene';
import { Tdm106SceneSim, type Tdm106SimCmd } from '@/lib/teleop/tdm106SceneSim';
import { useTdm106Drive, type Tdm106Block, type Tdm106DriveCmd } from '@/lib/teleop/useTdm106Drive';
import { introSeen, markIntroSeen, saveReadiness, useTdm59Readiness } from '@/lib/teleop/tdm59Readiness';
import { GRADE_STYLE, runNetworkTest, type NetworkTest } from '@/lib/teleop/tdm59Quality';

const RobotTwin3D = dynamic(() => import('@/components/robotview/RobotTwin3D').then((m) => m.RobotTwin3D), {
  ssr: false,
  loading: () => <div className="flex h-full w-full items-center justify-center text-xs text-ink-muted">Loading 3D view…</div>,
});

const PAUSE_AT_MS = 400; // same as the TDM-56 console's default maxLatencyMs
const IK = createTdm106Ik(createYamKinematics(YAM_MODEL), YAM_BODIES);

/** The main practice view: high above and behind the base, looking down the table, so W is "up the
 *  screen", A is left and D is right, and the arm's own links hide as little of the table as possible. */
const DRIVER_CAM = { id: 'tdm106-driver', pos: [0.04, 0, 1.0] as V3, target: [0.34, 0, 0.0] as V3, fovy: 44 };

type LessonId = 'move' | 'updown' | 'grip' | 'stop' | 'turn';
interface Lesson {
  id: LessonId;
  title: string;
  key: string;
  touch: string;
  keys: string[];
}
const LESSONS: Lesson[] = [
  { id: 'move', title: 'Move the hand', key: 'Hold W, A, S or D. Let go to stop.', touch: 'Drag the Move stick.', keys: ['KeyW', 'KeyA', 'KeyS', 'KeyD'] },
  { id: 'updown', title: 'Go up and down', key: 'Hold Q to go up, E to go down.', touch: 'Drag the Up / down stick.', keys: ['KeyQ', 'KeyE'] },
  { id: 'grip', title: 'Use the gripper', key: 'Tap G to close it, tap G again to open it.', touch: 'Tap Close grip, then Open grip.', keys: ['KeyG'] },
  { id: 'stop', title: 'Press STOP', key: 'Press Esc (or the red STOP button), then Resume.', touch: 'Tap STOP, then Resume.', keys: ['Escape'] },
];
const BASICS = LESSONS.map((l) => l.id);
/** Once these pass, "Skip to the task" appears (the gripper is learnt in the task anyway; STOP comes after it). */
const SKIP_AFTER: LessonId[] = ['move', 'updown'];

const LAGS = [
  { ms: 0, label: 'None' },
  { ms: 150, label: '150 ms' },
  { ms: 300, label: '300 ms' },
  { ms: 500, label: '500 ms' },
];

function titleCase(s: string) {
  return s.replace(/_/g, ' ');
}

export function PracticeConsole({ next, taskText, scene: sceneFetch }: { next?: string; taskText?: string | null; scene?: Tdm106SceneFetch | null }) {
  const source = useMemo(() => tdm106PracticeScene(sceneFetch ?? null), [sceneFetch]);
  const spec: SceneSpecV1 = useMemo(() => ({ ...source.spec, cameras: [...(source.spec.cameras ?? []), DRIVER_CAM] }), [source.spec]);
  const simRef = useRef<Tdm106SceneSim | null>(null);
  if (!simRef.current || simRef.current.spec !== spec) simRef.current = new Tdm106SceneSim({ spec, ik: IK, home: YAM_READY });
  const sim = simRef.current;
  const objName = titleCase(sim.task?.obj ?? 'object');
  const goalName = titleCase(sim.task?.goal ?? 'goal');

  const [touch, setTouch] = useState(false);
  const [stickSize, setStickSize] = useState(112);
  const [wide, setWide] = useState(true); // lg: the legend sits at the top of the side column
  useEffect(() => {
    setTouch(window.matchMedia?.('(pointer: coarse)').matches ?? false);
    const fit = () => {
      setStickSize(window.innerWidth < 420 ? 100 : 112);
      setWide(window.innerWidth >= 1024);
    };
    fit();
    window.addEventListener('resize', fit);
    return () => window.removeEventListener('resize', fit);
  }, []);

  // ---- simulated network delay
  const [lag, setLag] = useState(0);
  const lagRef = useRef(0);
  lagRef.current = lag;
  const queue = useRef<{ at: number; cmd: Tdm106SimCmd }[]>([]);

  // ---- STOP
  const [stopped, setStopped] = useState(false);
  const stoppedRef = useRef(false);
  stoppedRef.current = stopped;
  const pressStop = useCallback(() => {
    stoppedRef.current = true;
    setStopped(true);
    queue.current = [];
    simRef.current?.stop();
  }, []);
  const resume = useCallback(() => {
    stoppedRef.current = false;
    setStopped(false);
    simRef.current?.resume();
  }, []);

  // ---- "How to drive" walkthrough: on demand only (the on-screen hints teach as you go)
  const [intro, setIntro] = useState(false);
  const introRef = useRef(false);
  introRef.current = intro;
  const closeIntro = useCallback(() => {
    markIntroSeen('practice');
    setIntro(false);
  }, []);

  // ---- lessons + task
  const [done, setDone] = useState<Record<LessonId, boolean>>({ move: false, updown: false, grip: false, stop: false, turn: false });
  const doneRef = useRef(done);
  doneRef.current = done;
  const tick = useCallback((id: LessonId) => {
    if (doneRef.current[id]) return;
    doneRef.current = { ...doneRef.current, [id]: true };
    setDone(doneRef.current);
  }, []);
  const [skipped, setSkipped] = useState(false);
  const [taskDone, setTaskDone] = useState(false);
  const taskStart = useRef<number | null>(null);
  const [taskMs, setTaskMs] = useState<number | null>(null);
  const [bestMs, setBestMs] = useState<number | null>(null);
  const practiceStart = useRef<number | null>(null);
  const stopSeen = useRef(false);
  const gripSeen = useRef({ closed: false });
  // how far the hand has travelled (all strokes), for the move / up-down lessons
  const travel = useRef({ xy: 0, z: 0, last: null as V3 | null });

  const [toast, setToast] = useState<{ text: string; tone: 'good' | 'warn'; at: number } | null>(null);
  const say = (text: string, tone: 'good' | 'warn') => setToast({ text, tone, at: Date.now() });
  useEffect(() => {
    if (!toast) return;
    const id = window.setTimeout(() => setToast(null), 3500);
    return () => window.clearTimeout(id);
  }, [toast]);

  const blockReason = useCallback((): Tdm106Block => {
    if (stoppedRef.current) return { code: 'stop', text: 'Stopped. Press Resume when you are ready.' };
    if (introRef.current) return { code: 'intro', text: 'Close the walkthrough to start driving.' };
    if (lagRef.current > PAUSE_AT_MS) return { code: 'latency', text: `Video delay is ${lagRef.current} ms (limit ${PAUSE_AT_MS} ms). Driving is paused, just like on the real robot.` };
    if (typeof document !== 'undefined' && document.hidden) return { code: 'hidden', text: 'Tab is in the background.' };
    return null;
  }, []);

  const send = useCallback((cmd: Tdm106DriveCmd) => {
    queue.current.push({ at: performance.now() + lagRef.current, cmd });
    if (cmd.clutch) {
      practiceStart.current ??= Date.now();
      if (Math.abs(cmd.d_rot[2]) >= (20 * Math.PI) / 180) tick('turn');
      if (taskStart.current == null && !simRef.current?.succeeded) taskStart.current = Date.now();
    }
    return true;
  }, [tick]);

  const drive = useTdm106Drive({
    blockReason,
    send,
    pressStop,
    keysPaused: () => introRef.current,
    assistTarget: () => {
      const s = simRef.current;
      const o = s?.taskObject();
      return s && o && !o.held ? { tool: s.tool, obj: o.pos } : null;
    },
    afterTick: ({ now, dt }) => {
      const s = simRef.current;
      if (!s) return;
      const q = queue.current;
      while (q.length && q[0].at <= now) s.command(q.shift()!.cmd, now);
      s.step(dt, now);
      const tr = travel.current;
      if (tr.last) {
        tr.xy += Math.hypot(s.tool[0] - tr.last[0], s.tool[1] - tr.last[1]);
        tr.z += Math.abs(s.tool[2] - tr.last[2]);
        if (tr.xy >= 0.04) tick('move');
        if (tr.z >= 0.03) tick('updown');
      }
      tr.last = [...s.tool] as V3;
      for (const ev of s.drainEvents()) {
        if (ev.k === 'grasp') say(`Got it. Lift it (Q) and carry it to the ${goalName}.`, 'good');
        else if (ev.k === 'missed_grasp') say(ev.why, 'warn');
        else if (ev.k === 'drop') {
          if (ev.success) {
            const ms = taskStart.current != null ? Date.now() - taskStart.current : null;
            setTaskDone(true);
            setTaskMs(ms);
            if (ms != null) setBestMs((b) => (b == null || ms < b ? ms : b));
            say(`On the ${goalName}${ms != null ? ` in ${fmtTime(ms)}` : ''}. That is a clean demo.`, 'good');
          } else say(`Dropped ${ev.where === 'other' ? `next to the ${goalName}` : 'on the table'}. Pick it up again.`, 'warn');
        } else if (ev.k === 'stop') stopSeen.current = true;
        else if (ev.k === 'resume' && stopSeen.current) tick('stop');
      }
    },
  });

  // gripper lesson: closed once, then opened again
  useEffect(() => {
    if (drive.gripper < 0.5) gripSeen.current.closed = true;
    else if (gripSeen.current.closed) tick('grip');
  }, [drive.gripper, tick]);

  // ---- the coach + task clock, refreshed with the 10 Hz UI snapshot
  const [guide, setGuide] = useState<Tdm106Guide | null>(null);
  const [clock, setClock] = useState(0);
  const [hud, setHud] = useState({ z: 0.2, clamps: [] as string[], watchdog: false });
  useEffect(() => {
    const id = window.setInterval(() => {
      const s = simRef.current;
      if (!s) return;
      const o = s.taskObject();
      setGuide(
        tdm106Guide({
          tool: s.tool,
          gripperOpen: s.gripperCmd > 0.5,
          gripperMoving: Math.abs(s.jaw - s.gripperCmd) > 0.02 && !o?.held,
          obj: o,
          goal: s.goal(),
          done: s.succeeded,
          objName,
          goalName,
        }),
      );
      setHud({ z: s.tool[2] - s.spec.table.top_z, clamps: s.clamps, watchdog: s.watchdog });
      setClock(taskStart.current != null && !s.succeeded ? Date.now() - taskStart.current : 0);
    }, 100);
    return () => window.clearInterval(id);
  }, [objName, goalName]);

  const resetScene = useCallback(
    (newLayout = false) => {
      const s = simRef.current;
      if (!s) return;
      s.reset(newLayout ? Math.floor(Math.random() * 1e9) : undefined);
      s.drainEvents();
      queue.current = [];
      drive.resetInputs();
      drive.setGripper(1);
      setTaskDone(false);
      setTaskMs(null);
      taskStart.current = null;
    },
    [drive],
  );

  // ---- network test (part of the readiness check)
  const [net, setNet] = useState<NetworkTest | null>(null);
  const [netBusy, setNetBusy] = useState(false);
  const testNet = useCallback(async () => {
    setNetBusy(true);
    try {
      setNet(await runNetworkTest(10));
    } finally {
      setNetBusy(false);
    }
  }, []);
  useEffect(() => {
    void testNet();
    // first visit: no blocking walkthrough any more, but keep the "seen" flag for the teleop page
    if (!introSeen('practice')) markIntroSeen('practice');
  }, [testNet]);

  // ---- readiness
  const basicsDone = BASICS.every((l) => done[l]);
  const netOk = net != null && net.advice.grade !== 'bad';
  const ready = basicsDone && taskDone && netOk;
  const { status: readyStatus } = useTdm59Readiness();
  const saved = useRef(false);
  useEffect(() => {
    if (!ready || saved.current) return;
    saved.current = true;
    saveReadiness({
      practiceMs: practiceStart.current ? Date.now() - practiceStart.current : 0,
      lessons: [...LESSONS.filter((l) => done[l.id]).map((l) => l.id), 'pick_place'],
      network: net ? { medianMs: net.medianMs, jitterMs: net.jitterMs, grade: net.advice.grade, type: net.net?.type } : null,
    });
  }, [ready, net, done]);

  // coach order: the basics, unless skipped; then the task; then any basic still missing (STOP)
  const pending = LESSONS.find((l) => !done[l.id]) ?? null;
  const inTask = !pending || (skipped && !taskDone);
  const lesson = inTask ? null : pending;
  const canSkip = !inTask && !taskDone && SKIP_AFTER.every((l) => done[l]);
  const coach = lesson
    ? { kind: 'lesson' as const, n: LESSONS.indexOf(lesson) + 1, title: lesson.title, detail: touch ? lesson.touch : lesson.key, keys: lesson.keys }
    : guide
      ? { kind: 'task' as const, n: guide.n, title: guide.title, detail: guide.detail, keys: guide.keys, step: guide.step }
      : null;
  const hintKeys = coach?.keys ?? [];

  const ui = drive.ui;
  const status = useMemo(() => {
    if (ui.engaged) return { tone: 'drive' as const, title: 'Driving', sub: ui.source === 'gripper' ? 'Moving the gripper. The arm holds still.' : 'Let go of the keys to hold still.' };
    if (ui.block && ui.block.code !== 'intro') return { tone: ui.want || ui.needsRelease ? ('blocked' as const) : ('idle' as const), title: ui.want ? 'Paused for safety' : 'Not driving', sub: ui.block.text };
    if (ui.needsRelease) return { tone: 'blocked' as const, title: 'Let go of the keys', sub: 'Then press them again to drive.' };
    return { tone: 'idle' as const, title: 'Holding still', sub: touch ? 'Drag a stick to move the hand.' : 'Hold W A S D to move, Q / E for up and down, tap G to grip.' };
  }, [ui, touch]);

  const sceneLabel =
    source.from === 'ticket'
      ? `Ticket ${source.ticketId}'s scene${source.revision ? ` (rev ${source.revision})` : ''}`
      : source.from === 'robot'
        ? `Scene deployed on sim robot ${source.robotId}`
        : 'Default sim scene';

  return (
    <div style={{ ['--tdm56-drive' as string]: 'var(--melt)' }} data-testid="tdm59-practice">
      {/* ---------------- top bar, same shape as the real console */}
      <div className="sticky top-0 z-30 -mx-4 mb-3 border-b border-zinc-200 bg-zinc-50/95 px-4 py-2 backdrop-blur dark:border-zinc-800 dark:bg-zinc-950/95">
        <div className="flex items-center gap-3">
          <div
            className={`flex min-w-0 flex-1 items-center gap-3 rounded-xl px-3 py-2 transition-colors ${
              status.tone === 'drive'
                ? 'bg-melt text-on-melt'
                : status.tone === 'blocked'
                  ? 'bg-red-50 text-red-900 ring-1 ring-red-200 dark:bg-red-950/50 dark:text-red-100 dark:ring-red-900'
                  : 'bg-zinc-100 text-zinc-800 dark:bg-zinc-900 dark:text-zinc-200'
            }`}
            aria-live="polite"
            data-testid="tdm59-practice-status"
            data-state={status.tone}
          >
            <span className={`inline-block h-3 w-3 shrink-0 rounded-full ${status.tone === 'drive' ? 'animate-pulse bg-on-melt' : status.tone === 'blocked' ? 'bg-red-600' : 'bg-zinc-400'}`} />
            <div className="min-w-0">
              <div className="truncate text-base font-semibold leading-tight sm:text-lg">{status.title}</div>
              <div className="truncate text-xs opacity-80 sm:text-sm">{status.sub}</div>
            </div>
          </div>
          <button
            type="button"
            onClick={pressStop}
            onPointerDown={(e) => {
              e.preventDefault();
              pressStop();
            }}
            className="h-16 shrink-0 rounded-2xl border-4 border-red-900/30 bg-[#d4221a] px-6 text-2xl font-black tracking-wide text-white shadow-[0_4px_0_#7f1410] active:translate-y-1 active:shadow-none focus-visible:outline focus-visible:outline-4 focus-visible:outline-offset-2 focus-visible:outline-red-600 sm:h-20 sm:px-10 sm:text-3xl"
            aria-label="Stop the practice arm (Escape)"
            data-testid="tdm59-practice-stop"
          >
            STOP
          </button>
        </div>
        {stopped && (
          <div className="mt-2 flex flex-wrap items-center gap-3 rounded-lg bg-red-600 px-3 py-2 text-sm text-white" role="alert">
            <span className="font-semibold">Stopped. On the real robot it stays locked like this until you resume.</span>
            <button type="button" className="rounded-md bg-white px-3 py-1 font-medium text-red-700 hover:bg-red-50" onClick={resume} data-testid="tdm59-practice-resume">
              Resume
            </button>
          </div>
        )}
      </div>

      {/* ---------------- coach: the one next thing to do */}
      <div
        className={`mb-3 flex flex-wrap items-center gap-x-4 gap-y-2 rounded-sm px-4 py-3 ring-1 ${
          coach?.kind === 'task' && coach.step === 'done'
            ? 'bg-arc-soft ring-arc'
            : 'bg-surface ring-line-strong'
        }`}
        data-testid="tdm59-coach"
        data-kind={coach?.kind}
        data-step={coach?.kind === 'task' ? coach.step : lesson?.id}
      >
        {coach && (
          <div className="flex min-w-0 grow basis-72 items-center gap-3">
            <span className="gx-tag shrink-0 tabular-nums">
              {coach.kind === 'lesson' ? `Basics ${coach.n}/${BASICS.length}` : `Task ${Math.min(coach.n, 5)}/5`}
            </span>
            <div className="min-w-0">
              <div className="gx-heading leading-tight">{coach.title}</div>
              <div className="text-[15px] leading-snug text-ink-muted">{coach.detail}</div>
            </div>
          </div>
        )}
        <div className="flex flex-wrap items-center gap-2 text-sm">
          {coach?.kind === 'task' && (
            <span className="gx-data text-ink-muted" data-testid="tdm106-task-time">
              {taskDone && taskMs != null ? `Done in ${fmtTime(taskMs)}` : clock ? `Task time ${fmtTime(clock)}` : 'Clock starts when you move'}
              {bestMs != null && ` · best ${fmtTime(bestMs)}`}
            </span>
          )}
          {canSkip && (
            <button type="button" onClick={() => setSkipped(true)} className="gx-btn gx-btn-primary gx-btn-sm" data-testid="tdm106-skip-to-task">
              Skip to the task
            </button>
          )}
          {taskDone && (
            <button type="button" onClick={() => resetScene(true)} className="gx-btn gx-btn-sm" data-testid="tdm106-again">
              Go again (new layout)
            </button>
          )}
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_330px]">
        <div className="min-w-0">
          {/* ---------------- 3D views of the scene */}
          <div className={`relative rounded-xl p-1 transition-colors ${ui.engaged ? 'bg-melt' : 'bg-transparent'}`} data-testid="tdm59-practice-views">
            <div className="grid grid-cols-3 gap-1 sm:h-[min(46vh,460px)] sm:grid-rows-2">
              <View label="Driver's view (from behind the robot)" lagMs={lag} className="col-span-3 aspect-[16/10] sm:col-span-2 sm:row-span-2 sm:aspect-auto">
                <PracticeTwin sim={sim} spec={spec} camera={DRIVER_CAM.id} lagMs={lag} controls ghost />
              </View>
              <View label="Side" lagMs={lag} compact className="aspect-[4/3] sm:aspect-auto">
                <PracticeTwin sim={sim} spec={spec} camera="side" lagMs={lag} ghost />
              </View>
              <View label="Wrist camera" lagMs={lag} compact className="aspect-[4/3] sm:aspect-auto">
                <PracticeTwin sim={sim} spec={spec} camera="wrist" lagMs={lag} />
              </View>
              <div className="flex flex-col justify-center gap-1 rounded-sm bg-surface-sunken px-2 py-1.5 text-[11px] leading-snug text-ink-muted sm:hidden">
                <span>
                  <b className="text-arc">Blue ghost</b>: where the hand lands if you lower straight down.
                </span>
              </div>
            </div>
            {toast && (
              <div
                className={`pointer-events-none absolute inset-x-0 bottom-4 mx-auto w-fit max-w-[90%] rounded-full px-4 py-2 text-sm font-semibold shadow-lg ${toast.tone === 'good' ? 'bg-arc text-on-arc' : 'bg-zinc-900/90 text-white'}`}
                role="status"
                data-testid="tdm59-toast"
              >
                {toast.text}
              </div>
            )}
          </div>

          <div className="mt-2 flex flex-wrap items-center justify-between gap-2 text-sm">
            <div className="flex flex-wrap items-center gap-x-1">
              <SimTag label={`Simulated · ${sceneLabel}`} className="mr-1" />
              <span className="sr-only" data-testid="tdm106-sim-label">{sceneLabel}</span>
              <button type="button" className="rounded-md px-2 py-1 text-zinc-600 underline-offset-2 hover:underline dark:text-zinc-400" onClick={() => resetScene(false)} data-testid="tdm59-reset-scene">
                Reset the scene
              </button>
              <button type="button" className="rounded-md px-2 py-1 text-zinc-600 underline-offset-2 hover:underline dark:text-zinc-400" onClick={() => resetScene(true)}>
                New layout
              </button>
              <button type="button" className="rounded-md px-2 py-1 text-zinc-600 underline-offset-2 hover:underline dark:text-zinc-400" onClick={() => setIntro(true)}>
                How to drive
              </button>
            </div>
            <span className="text-xs tabular-nums text-zinc-500">
              <span className="hidden sm:inline">
                <b className="text-arc">Blue ghost</b> = where the hand lands if you lower straight down ·{' '}
              </span>
              Hand {Math.round(hud.z * 100)} cm above the table
              {hud.clamps.includes('clamped_workspace') && <Chip>Workspace edge</Chip>}
              {hud.clamps.includes('table') && <Chip>Table</Chip>}
              {hud.clamps.includes('reach') && <Chip>Out of reach</Chip>}
              {hud.watchdog && <span className="ml-2 rounded bg-amber-500 px-1.5 py-0.5 font-semibold text-black">Watchdog hold</span>}
            </span>
          </div>
          {source.note && (
            <p className="mt-2 rounded-md bg-amber-50 px-3 py-1.5 text-xs text-amber-900 ring-1 ring-amber-200 dark:bg-amber-950/40 dark:text-amber-100 dark:ring-amber-900" data-testid="tdm106-scene-note">
              {source.note}
            </p>
          )}

          {/* ---------------- the controls, always on screen */}
          <div className="mt-3 grid gap-3">
            {!touch && !wide && <Tdm106KeyLegend drive={drive} hint={hintKeys} />}
            <Tdm106OnScreenControls drive={drive} stickSize={stickSize} testPrefix="tdm59" />
            {touch && <Tdm106KeyLegend drive={drive} hint={hintKeys} />}
          </div>
        </div>

        {/* ---------------- side panel */}
        <aside className="flex flex-col gap-4 text-sm">
          {!touch && wide && <Tdm106KeyLegend drive={drive} hint={hintKeys} />}
          <Panel title="Readiness check" aside={<span className="tabular-nums text-zinc-500">{BASICS.filter((l) => done[l]).length + (taskDone ? 1 : 0) + (netOk ? 1 : 0)}/{BASICS.length + 2}</span>}>
            <ol className="space-y-1" data-testid="tdm59-lessons">
              {LESSONS.map((l, i) => (
                <Step key={l.id} n={i + 1} on={done[l.id]} current={!inTask && lesson?.id === l.id} id={l.id}>
                  {l.title}
                </Step>
              ))}
              <Step n={BASICS.length + 1} on={taskDone} current={inTask && !taskDone} id="pick_place">
                Task: put the {objName} on the {goalName}
              </Step>
              <li className="flex items-start gap-2 rounded-md px-1.5 py-1" data-done={netOk}>
                <Check on={netOk} n={BASICS.length + 2} />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className={netOk ? 'text-zinc-500' : 'font-medium'}>Your connection</span>
                    {net && (
                      <span className={`inline-flex items-center gap-1 text-xs ${GRADE_STYLE[net.advice.grade].text}`} data-testid="tdm59-net" data-grade={net.advice.grade}>
                        <span className={`h-2 w-2 rounded-full ${GRADE_STYLE[net.advice.grade].dot}`} />
                        {net.advice.headline}
                        {net.medianMs != null && ` · ${Math.round(net.medianMs)} ms`}
                      </span>
                    )}
                    <button type="button" onClick={() => void testNet()} disabled={netBusy} className="text-xs text-zinc-600 underline underline-offset-2 disabled:no-underline disabled:opacity-60 dark:text-zinc-400">
                      {netBusy ? 'Testing…' : 'Test again'}
                    </button>
                  </div>
                  {net && net.advice.grade !== 'good' && <p className="mt-0.5 text-xs text-zinc-600 dark:text-zinc-400">{net.advice.tips[0]}</p>}
                </div>
              </li>
            </ol>
            {ready || readyStatus === 'ready' ? (
              <div className="mt-3 rounded-lg bg-emerald-50 p-3 ring-1 ring-emerald-600/25 dark:bg-emerald-950/40" data-testid="tdm59-ready">
                <p className="font-semibold text-emerald-900 dark:text-emerald-100">You are ready to drive the real robot.</p>
                <p className="mt-0.5 text-xs text-emerald-900/80 dark:text-emerald-100/80">Claiming teleop and takeover tasks is unlocked on this device.</p>
                <Link href={next ?? '/marketplace'} className="mt-2 inline-block rounded-md bg-emerald-700 px-4 py-2 font-semibold text-white hover:bg-emerald-800" data-testid="tdm59-continue">
                  {next ? 'Back to the task' : 'Find a paid task'}
                </Link>
              </div>
            ) : (
              <p className="mt-2 text-xs text-zinc-500">Four quick basics and the task once. That unlocks claiming paid teleop tasks.</p>
            )}
          </Panel>

          <Panel title="Driving">
            <Tdm106DriveSettings drive={drive} snapAvailable snapNote={`Near the ${objName}, the hand is pulled toward its centre as you move, so lowering lines up by itself. Turn it off to practise without help.`} />
          </Panel>

          <details className="gx-plate">
            <summary className="flex cursor-pointer items-center justify-between gap-2 text-sm font-semibold">
              Feel the lag
              <LatencyBadge videoMs={lag} cmdHz={ui.hz} driving={ui.engaged} simulated pauseAtMs={PAUSE_AT_MS} />
            </summary>
            <div className="mt-2 flex gap-1" role="group" aria-label="Simulated video delay">
              {LAGS.map((l) => (
                <button
                  key={l.ms}
                  type="button"
                  onClick={() => setLag(l.ms)}
                  aria-pressed={lag === l.ms}
                  className={`flex-1 rounded-md px-2 py-1.5 text-xs font-medium ring-1 ${
                    lag === l.ms ? 'bg-zinc-900 text-white ring-zinc-900 dark:bg-zinc-100 dark:text-zinc-900' : 'ring-zinc-300 hover:bg-zinc-100 dark:ring-zinc-700 dark:hover:bg-zinc-800'
                  }`}
                  data-testid={`tdm59-lag-${l.ms}`}
                >
                  {l.label}
                </button>
              ))}
            </div>
            <p className="mt-2 text-xs text-zinc-500">
              Adds a delay to everything you send, like a slow network. Under 150 ms feels smooth; over 300 ms is hard to drive; over 400 ms the real page pauses driving.
            </p>
          </details>
        </aside>
      </div>

      <InstructionsOverlay open={intro} onClose={closeIntro} kind="practice" taskText={taskText ?? source.spec.task} doneLabel="Back to practice" />
    </div>
  );
}

// ------------------------------------------------------------------ the twin, fed from the sim at ~30 Hz

function PracticeTwin({ sim, spec, camera, lagMs, controls = false, ghost = false }: { sim: Tdm106SceneSim; spec: SceneSpecV1; camera: string; lagMs: number; controls?: boolean; ghost?: boolean }) {
  const [state, setState] = useState<RobotLiveState | null>(null);
  const [rows, setRows] = useState<number[][] | undefined>(undefined);
  const history = useRef<{ at: number; s: RobotLiveState }[]>([]);
  const lagRef = useRef(lagMs);
  lagRef.current = lagMs;
  useEffect(() => {
    let n = 0;
    const id = window.setInterval(() => {
      const now = performance.now();
      const s = sim.liveState(Date.now());
      // the simulated network delay applies to the picture too (as video would lag)
      const h = history.current;
      h.push({ at: now, s });
      while (h.length > 2 && h[1].at <= now - lagRef.current) h.shift();
      setState(lagRef.current ? h[0].s : s);
      if (ghost && n++ % 3 === 0) {
        const land = sim.landingJoints();
        setRows(land ? [s.joints, land] : undefined);
      }
    }, 33);
    return () => window.clearInterval(id);
  }, [sim, ghost]);
  return <RobotTwin3D state={state} spec={spec} camera={camera} sim controls={controls} ghost={rows} className="h-full w-full" />;
}

function View({ label, lagMs, compact, className = '', children }: { label: string; lagMs: number; compact?: boolean; className?: string; children: ReactNode }) {
  const tone = lagMs < 150 ? 'bg-emerald-600/85 text-white' : lagMs <= 300 ? 'bg-amber-500/90 text-black' : 'bg-red-600/90 text-white';
  return (
    <div className={`relative overflow-hidden rounded-lg bg-[#2f2c40] ${className}`}>
      {children}
      <span className={`pointer-events-none absolute left-2 top-2 rounded-md bg-black/55 px-1.5 py-0.5 font-medium text-white ${compact ? 'text-[10px] sm:text-xs' : 'text-xs'}`}>{label}</span>
      {lagMs > 0 && (
        <span className={`pointer-events-none absolute right-2 top-2 rounded-md px-1.5 py-0.5 text-[10px] font-semibold tabular-nums sm:text-xs ${tone}`}>{lagMs} ms</span>
      )}
    </div>
  );
}

function Chip({ children }: { children: ReactNode }) {
  return <span className="ml-2 rounded-sm bg-ochre-soft px-1.5 py-0.5 text-ink">{children}</span>;
}

function Step({ n, on, current, id, children }: { n: number; on: boolean; current: boolean; id: string; children: ReactNode }) {
  return (
    <li className={`flex items-center gap-2 rounded-md px-1.5 py-1 ${current ? 'bg-surface-sunken' : ''}`} data-done={on} data-lesson={id}>
      <Check on={on} n={n} />
      <span className={on ? 'text-zinc-500 line-through decoration-zinc-400/60' : current ? 'font-semibold' : ''}>{children}</span>
    </li>
  );
}

function Check({ on, n }: { on: boolean; n: number }) {
  return (
    <span
      className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-[11px] font-bold ${on ? 'bg-emerald-600 text-white' : 'bg-zinc-100 text-zinc-500 ring-1 ring-zinc-300 dark:bg-zinc-800 dark:ring-zinc-700'}`}
      aria-label={on ? 'done' : 'not done'}
    >
      {on ? (
        <svg viewBox="0 0 12 12" className="h-3 w-3" aria-hidden>
          <path d="M2.5 6.5l2.2 2.2 4.8-5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      ) : (
        n
      )}
    </span>
  );
}

function Panel({ title, aside, children }: { title: string; aside?: ReactNode; children: ReactNode }) {
  return (
    <section className="gx-plate">
      <div className="mb-2 flex items-center justify-between gap-2">
        <h2 className="gx-label uppercase">{title}</h2>
        {aside}
      </div>
      {children}
    </section>
  );
}

function fmtTime(ms: number) {
  const s = Math.round(ms / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}
