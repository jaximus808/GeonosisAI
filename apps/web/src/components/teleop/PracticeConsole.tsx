'use client';

// TDM-59: practice mode. The TDM-56 controls (same keys, gamepad mapping, on-screen sticks, clutch,
// speed / slow mode, gripper, STOP) drive a simulated arm in the browser instead of the robot.
// No robot link, no claim or lease, no recording: nothing leaves this page except the small
// network test request to /api/tdm59/ping.
//
// Passing every lesson plus the network test is the readiness check that unlocks claiming
// arm-control tasks (lib/teleop/tdm59Readiness.ts, <Tdm59ClaimGate>).

import Link from 'next/link';
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { InstructionsOverlay } from '@/components/teleop/InstructionsOverlay';
import { LatencyBadge } from '@/components/teleop/LatencyBadge';
import { Tdm56Joystick, type Tdm56StickValue } from '@/components/teleop/Tdm56Joystick';
import { SIM_VIEW_ASPECT, Tdm59SimView } from '@/components/teleop/Tdm59SimView';
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
import { introSeen, markIntroSeen, saveReadiness, useTdm59Readiness } from '@/lib/teleop/tdm59Readiness';
import { GRADE_STYLE, runNetworkTest, type NetworkTest } from '@/lib/teleop/tdm59Quality';
import { Tdm59SimArm, type SimCmd } from '@/lib/teleop/tdm59Sim';

const TICK_MS = 33;
const SLOW_CAP = 0.25;
const GRIP_RATE = 0.9;
const PAUSE_AT_MS = 400; // same as the TDM-56 console's default maxLatencyMs

type LessonId = 'clutch' | 'release' | 'lift' | 'turn' | 'grip' | 'place' | 'stop';
const LESSON_IDS: LessonId[] = ['clutch', 'release', 'lift', 'turn', 'grip', 'place', 'stop'];

interface Lesson {
  id: LessonId;
  title: string;
  key: string; // keyboard hint
  touch: string; // phone hint
}

const LESSONS: Lesson[] = [
  { id: 'clutch', title: 'Drive with the clutch', key: 'Hold Space and press W to move the hand forward.', touch: 'Put your thumb on the Move stick and push it. Touching a stick holds the clutch.' },
  { id: 'release', title: 'Let go to hold still', key: 'Let go of Space. The arm stays where it is.', touch: 'Lift your thumb off. The arm stays where it is.' },
  { id: 'lift', title: 'Lift and lower', key: 'Hold Space and press Q to go up or E to go down.', touch: 'Push the Lift and turn stick up or down.' },
  { id: 'turn', title: 'Turn the hand', key: 'Hold Space and press ← or →.', touch: 'Push the Lift and turn stick left or right.' },
  {
    id: 'grip',
    title: 'Pick up the blue block',
    key: 'Line the fingers up with the block, lower around it (E), then hold Space and F to close.',
    touch: 'Line the fingers up with the block, lower around it, then slide the gripper to Closed while holding the clutch.',
  },
  { id: 'place', title: 'Drop it in the bin', key: 'Carry it over the bin, then hold Space and R to open.', touch: 'Carry it over the bin, then slide the gripper to Open while holding the clutch.' },
  { id: 'stop', title: 'Press STOP, then resume', key: 'Press STOP or Esc, then Resume.', touch: 'Tap STOP, then Resume.' },
];

const LAGS = [
  { ms: 0, label: 'None' },
  { ms: 150, label: '150 ms' },
  { ms: 300, label: '300 ms' },
  { ms: 500, label: '500 ms' },
];

type Block = { code: string; text: string } | null;

export function PracticeConsole({ next, taskText }: { next?: string; taskText?: string | null }) {
  const sim = useRef(new Tdm59SimArm());
  const integ = useRef(new Tdm56ClutchIntegrator());

  // ---- inputs (same sources as the TDM-56 console)
  const keys = useRef(new Set<string>());
  const shift = useRef(false);
  const spaceHeld = useRef(false);
  const clutchBtn = useRef(false);
  const stickL = useRef<Tdm56StickValue>({ x: 0, y: 0, active: false });
  const stickR = useRef<Tdm56StickValue>({ x: 0, y: 0, active: false });
  const [stickEngages, setStickEngages] = useState(true);
  const stickEngagesRef = useRef(true);
  stickEngagesRef.current = stickEngages;
  const [showSticks, setShowSticks] = useState(false);
  const [touch, setTouch] = useState(false);
  const [stickSize, setStickSize] = useState(124);
  useEffect(() => {
    const c = window.matchMedia?.('(pointer: coarse)').matches ?? false;
    setShowSticks(c);
    setTouch(c);
    const fit = () => setStickSize(window.innerWidth < 420 ? 108 : 124);
    fit();
    window.addEventListener('resize', fit);
    return () => window.removeEventListener('resize', fit);
  }, []);

  const [gripper, setGripperState] = useState(1);
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

  const [lag, setLag] = useState(0);
  const lagRef = useRef(0);
  lagRef.current = lag;
  const queue = useRef<{ at: number; cmd: SimCmd }[]>([]);

  const [stopped, setStopped] = useState(false);
  const stoppedRef = useRef(false);
  stoppedRef.current = stopped;

  // ---- walkthrough (first visit), which also pauses input
  const [intro, setIntro] = useState(false);
  const introRef = useRef(false);
  introRef.current = intro;
  useEffect(() => {
    if (!introSeen('practice')) setIntro(true);
  }, []);
  const closeIntro = useCallback(() => {
    markIntroSeen('practice');
    setIntro(false);
  }, []);

  // ---- lessons
  const [done, setDone] = useState<Record<LessonId, boolean>>(() => Object.fromEntries(LESSON_IDS.map((l) => [l, false])) as Record<LessonId, boolean>);
  const doneRef = useRef(done);
  doneRef.current = done;
  const tick = useCallback((id: LessonId) => {
    if (doneRef.current[id]) return;
    doneRef.current = { ...doneRef.current, [id]: true };
    setDone(doneRef.current);
  }, []);
  const startedAt = useRef<number | null>(null);
  const stopSeen = useRef(false);
  const [toast, setToast] = useState<{ text: string; tone: 'good' | 'warn'; at: number } | null>(null);
  const say = (text: string, tone: 'good' | 'warn') => setToast({ text, tone, at: Date.now() });
  useEffect(() => {
    if (!toast) return;
    const id = window.setTimeout(() => setToast(null), 3500);
    return () => window.clearTimeout(id);
  }, [toast]);

  const pressStop = useCallback(() => {
    stoppedRef.current = true;
    setStopped(true);
    queue.current = [];
    sim.current.stop();
  }, []);
  const resume = useCallback(() => {
    stoppedRef.current = false;
    setStopped(false);
    sim.current.resume();
  }, []);

  // ---- loop state
  const engaged = useRef(false);
  const needsRelease = useRef(false);
  const seq = useRef(0);
  const lastTick = useRef(0);
  const padPrev = useRef({ a: false, b: false });
  const lastBlock = useRef<Block>(null);
  const lastWant = useRef(false);
  const lastSource = useRef<string | null>(null);
  const lastPad = useRef<string | null>(null);
  const sentTimes = useRef<number[]>([]);

  const blockReason = useCallback((): Block => {
    if (stoppedRef.current) return { code: 'stop', text: 'Stopped. Press Resume when you are ready.' };
    if (introRef.current) return { code: 'intro', text: 'Close the walkthrough to start driving.' };
    if (lagRef.current > PAUSE_AT_MS) return { code: 'latency', text: `Video delay is ${lagRef.current} ms (limit ${PAUSE_AT_MS} ms). Driving is paused, just like on the real robot.` };
    if (typeof document !== 'undefined' && document.hidden) return { code: 'hidden', text: 'Tab is in the background.' };
    return null;
  }, []);

  const send = useCallback((cmd: SimCmd) => {
    const now = performance.now();
    queue.current.push({ at: now + lagRef.current, cmd });
    if (cmd.clutch) sentTimes.current.push(now);
  }, []);

  const release = useCallback(() => {
    if (!engaged.current) return;
    engaged.current = false;
    send({
      seq: ++seq.current,
      clutch: false,
      d_pos: integ.current.dPos.map((v) => round(v)) as V3,
      d_rot: integ.current.dRot.map((v) => round(v)) as V3,
      gripper: round(gripperRef.current, 3),
      speed_scale: round(effSpeedRef.current, 3),
    });
    if (doneRef.current.clutch) tick('release');
  }, [send, tick]);

  useEffect(() => {
    lastTick.current = performance.now();
    const id = window.setInterval(() => {
      const now = performance.now();
      const dt = Math.min(0.1, (now - lastTick.current) / 1000);
      lastTick.current = now;

      const p = firstPad();
      const pad = p ? readPad(p) : null;
      lastPad.current = pad?.id ?? null;
      if (pad) {
        if (pad.pressedB && !padPrev.current.b) pressStop();
        if (pad.pressedA && !padPrev.current.a) setGripper(gripperRef.current > 0.5 ? 0 : 1);
        padPrev.current = { a: pad.pressedA, b: pad.pressedB };
      }
      let gRate = pad?.gripperRate ?? 0;
      if (keys.current.has('KeyR')) gRate += 1;
      if (keys.current.has('KeyF')) gRate -= 1;
      if (gRate) setGripper(gripperRef.current + Math.sign(gRate) * GRIP_RATE * dt);

      let twist: Tdm56Twist = keyboardTwist(keys.current, shift.current);
      if (pad) twist = addTwist(twist, pad.twist);
      const L = stickL.current;
      const R = stickR.current;
      if (L.active || R.active) twist = addTwist(twist, { lin: [deadzone(L.y), -deadzone(L.x), deadzone(R.y)], ang: [0, 0, -deadzone(R.x)] });

      const sources: string[] = [];
      if (pad?.clutch) sources.push('gamepad');
      if (spaceHeld.current) sources.push('keyboard');
      if (clutchBtn.current) sources.push('clutch button');
      if (stickEngagesRef.current && (L.active || R.active)) sources.push('stick');
      const want = sources.length > 0;
      lastWant.current = want;
      lastSource.current = sources[0] ?? null;

      const block = blockReason();
      lastBlock.current = block;
      if (block) {
        release();
        if (want) needsRelease.current = true;
      } else if (!want) {
        needsRelease.current = false;
        release();
      } else if (!needsRelease.current) {
        if (!engaged.current) {
          integ.current.reset();
          engaged.current = true;
          startedAt.current ??= Date.now();
        }
        integ.current.step(twist ?? ZERO_TWIST, dt, effSpeedRef.current);
        const dPos = integ.current.dPos;
        const dRot = integ.current.dRot;
        send({
          seq: ++seq.current,
          clutch: true,
          d_pos: dPos.map((v) => round(v)) as V3,
          d_rot: dRot.map((v) => round(v)) as V3,
          gripper: round(gripperRef.current, 3),
          speed_scale: round(effSpeedRef.current, 3),
        });
        if (Math.hypot(dPos[0], dPos[1], dPos[2]) >= 0.05) tick('clutch');
        if (Math.abs(dPos[2]) >= 0.04) tick('lift');
        if (Math.abs(dRot[2]) >= (20 * Math.PI) / 180) tick('turn');
      }

      // deliver commands whose (simulated) network delay has passed, then step the arm
      const q = queue.current;
      while (q.length && q[0].at <= now) sim.current.command(q.shift()!.cmd, now);
      sim.current.step(dt, now);
      for (const ev of sim.current.drainEvents()) {
        if (ev.k === 'grasp') {
          tick('grip');
          say('Got it. Now carry it over the bin.', 'good');
        } else if (ev.k === 'missed_grasp') say(ev.why, 'warn');
        else if (ev.k === 'drop') {
          if (ev.inBin) {
            tick('place');
            say('In the bin. That is a clean demo.', 'good');
          } else say('Dropped outside the bin. Pick it up again.', 'warn');
        } else if (ev.k === 'stop') stopSeen.current = true;
        else if (ev.k === 'resume' && stopSeen.current) tick('stop');
      }
      const st = sentTimes.current;
      while (st.length && now - st[0] > 1000) st.shift();
    }, TICK_MS);
    return () => {
      window.clearInterval(id);
      release();
    };
  }, [blockReason, release, pressStop, send, tick]);

  // ---- keyboard (same map as TDM-56)
  useEffect(() => {
    const typing = (t: EventTarget | null) =>
      t instanceof HTMLElement && (t.tagName === 'TEXTAREA' || (t.tagName === 'INPUT' && (t as HTMLInputElement).type !== 'range') || t.isContentEditable);
    const down = (e: KeyboardEvent) => {
      if (typing(e.target) || introRef.current) return;
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
        e.preventDefault();
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

  // ---- UI snapshot at 10 Hz
  const [ui, setUi] = useState({ engaged: false, want: false, needsRelease: false, block: null as Block, source: null as string | null, pad: null as string | null, hz: 0, z: 0.2, clamps: [] as string[], watchdog: false });
  useEffect(() => {
    const id = window.setInterval(() => {
      const s = sim.current.s;
      setUi({
        engaged: engaged.current,
        want: lastWant.current,
        needsRelease: needsRelease.current,
        block: lastBlock.current,
        source: lastSource.current,
        pad: lastPad.current,
        hz: sentTimes.current.length,
        z: s.ee[2],
        clamps: s.clamps,
        watchdog: s.watchdog,
      });
    }, 100);
    return () => window.clearInterval(id);
  }, []);

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
  }, [testNet]);

  // ---- readiness
  const lessonsDone = LESSON_IDS.filter((l) => done[l]).length;
  const allLessons = lessonsDone === LESSON_IDS.length;
  const netOk = net != null && net.advice.grade !== 'bad';
  const ready = allLessons && netOk;
  const { status: readyStatus } = useTdm59Readiness();
  const saved = useRef(false);
  useEffect(() => {
    if (!ready || saved.current) return;
    saved.current = true;
    saveReadiness({
      practiceMs: startedAt.current ? Date.now() - startedAt.current : 0,
      lessons: LESSON_IDS,
      network: net ? { medianMs: net.medianMs, jitterMs: net.jitterMs, grade: net.advice.grade, type: net.net?.type } : null,
    });
  }, [ready, net]);

  const current = LESSONS.find((l) => !done[l.id]) ?? null;
  const engagedRef = engaged;
  const status = useMemo(() => {
    if (ui.engaged) return { tone: 'drive' as const, title: 'Driving', sub: `Clutch held (${ui.source ?? 'input'}). Let go to hold still.` };
    if (ui.block && ui.block.code !== 'intro') return { tone: ui.want || ui.needsRelease ? ('blocked' as const) : ('idle' as const), title: ui.want ? 'Clutch released for safety' : 'Not driving', sub: ui.block.text };
    if (ui.needsRelease) return { tone: 'blocked' as const, title: 'Let go of the clutch', sub: 'Then press it again to drive.' };
    return { tone: 'idle' as const, title: 'Not driving', sub: touch ? 'Hold the clutch button or touch a stick to drive.' : 'Hold Space, RT or the on-screen clutch to drive.' };
  }, [ui, touch]);

  const resetScene = () => {
    sim.current.reset();
    sim.current.drainEvents();
    queue.current = [];
    integ.current.reset();
    setGripper(1);
  };

  return (
    <div style={{ ['--tdm56-drive' as string]: '#f0b400' }} data-testid="tdm59-practice">
      {/* ---------------- top bar, same shape as the real console */}
      <div className="sticky top-0 z-30 -mx-4 mb-3 border-b border-zinc-200 bg-zinc-50/95 px-4 py-2 backdrop-blur dark:border-zinc-800 dark:bg-zinc-950/95">
        <div className="flex items-center gap-3">
          <div
            className={`flex min-w-0 flex-1 items-center gap-3 rounded-xl px-3 py-2 transition-colors ${
              status.tone === 'drive'
                ? 'bg-[#f0b400] text-black'
                : status.tone === 'blocked'
                  ? 'bg-red-50 text-red-900 ring-1 ring-red-200 dark:bg-red-950/50 dark:text-red-100 dark:ring-red-900'
                  : 'bg-zinc-100 text-zinc-800 dark:bg-zinc-900 dark:text-zinc-200'
            }`}
            aria-live="polite"
            data-testid="tdm59-practice-status"
            data-state={status.tone}
          >
            <span className={`inline-block h-3 w-3 shrink-0 rounded-full ${status.tone === 'drive' ? 'animate-pulse bg-black' : status.tone === 'blocked' ? 'bg-red-600' : 'bg-zinc-400'}`} />
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

      {/* ---------------- coach line: what to do next */}
      <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-2" data-testid="tdm59-coach">
        {current ? (
          <p className="min-w-0 grow basis-64 text-[15px] leading-snug">
            <span className="mr-2 inline-flex h-6 min-w-6 items-center justify-center rounded-full bg-zinc-900 px-1.5 text-xs font-bold text-white tabular-nums dark:bg-zinc-100 dark:text-zinc-900">
              {lessonsDone + 1}/{LESSONS.length}
            </span>
            <b>{current.title}.</b> <span className="text-zinc-600 dark:text-zinc-300">{touch ? current.touch : current.key}</span>
          </p>
        ) : (
          <p className="min-w-0 grow basis-64 text-[15px] font-semibold text-emerald-700 dark:text-emerald-300">All lessons done. Keep practising as long as you like.</p>
        )}
        <span className="inline-flex items-center gap-1.5 rounded-full bg-sky-50 px-2.5 py-1 text-xs font-medium text-sky-900 ring-1 ring-sky-200 dark:bg-sky-950/40 dark:text-sky-200 dark:ring-sky-900">
          Simulated arm. Nothing is recorded or sent to a robot.
        </span>
      </div>

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_320px]">
        <div className="min-w-0">
          {/* ---------------- views: side by side from sm up; on a phone the side view is an inset */}
          <div className={`relative rounded-xl p-1 transition-colors ${ui.engaged ? 'bg-[#f0b400]' : 'bg-transparent'}`} data-testid="tdm59-practice-views">
            <div className="relative grid grid-cols-1 gap-1 sm:grid-cols-2">
              <SimFrame label="Top camera" lagMs={lag}>
                <Tdm59SimView sim={sim} kind="top" engagedRef={engagedRef} />
              </SimFrame>
              <div className="absolute right-2 top-9 w-[36%] overflow-hidden rounded-lg ring-2 ring-zinc-950/70 sm:static sm:w-auto sm:rounded-none sm:ring-0">
                <SimFrame label="Side camera" lagMs={lag} compact>
                  <Tdm59SimView sim={sim} kind="side" engagedRef={engagedRef} />
                </SimFrame>
              </div>
            </div>
            {toast && (
              <div
                className={`pointer-events-none absolute inset-x-0 bottom-4 mx-auto w-fit max-w-[90%] rounded-full px-4 py-2 text-sm font-semibold shadow-lg ${
                  toast.tone === 'good' ? 'bg-emerald-600 text-white' : 'bg-zinc-900/90 text-white'
                }`}
                role="status"
                data-testid="tdm59-toast"
              >
                {toast.text}
              </div>
            )}
          </div>

          <div className="mt-2 flex flex-wrap items-center justify-between gap-2 text-sm">
            <div className="flex flex-wrap items-center gap-x-1 whitespace-nowrap">
              <button type="button" className="rounded-md px-2 py-1 text-zinc-600 underline-offset-2 hover:underline dark:text-zinc-400" onClick={() => setShowSticks((s) => !s)}>
                {showSticks ? 'Hide on-screen controls' : 'Show on-screen controls'}
              </button>
              <button type="button" className="rounded-md px-2 py-1 text-zinc-600 underline-offset-2 hover:underline dark:text-zinc-400" onClick={resetScene} data-testid="tdm59-reset-scene">
                Reset the scene
              </button>
              <button type="button" className="rounded-md px-2 py-1 text-zinc-600 underline-offset-2 hover:underline dark:text-zinc-400" onClick={() => setIntro(true)}>
                How to drive
              </button>
            </div>
            <span className="text-xs tabular-nums text-zinc-500">
              Hand {Math.round(ui.z * 100)} cm above the table
              {ui.clamps.includes('clamped_workspace') && <span className="ml-2 rounded bg-amber-100 px-1.5 py-0.5 text-amber-900 dark:bg-amber-950 dark:text-amber-200">Workspace edge</span>}
              {ui.clamps.includes('table') && <span className="ml-2 rounded bg-amber-100 px-1.5 py-0.5 text-amber-900 dark:bg-amber-950 dark:text-amber-200">Table</span>}
              {ui.watchdog && <span className="ml-2 rounded bg-amber-500 px-1.5 py-0.5 font-semibold text-black">Watchdog hold</span>}
            </span>
          </div>

          {showSticks && (
            <div className="sticky bottom-0 z-20 -mx-4 mt-2 border-t border-zinc-200 bg-zinc-50/95 px-4 py-3 backdrop-blur dark:border-zinc-800 dark:bg-zinc-950/95 sm:static sm:mx-0 sm:rounded-xl sm:border">
              <div className="flex items-end justify-between gap-2">
                <Tdm56Joystick label="Move" hint="forward, back, left, right" onChange={(v) => (stickL.current = v)} size={stickSize} />
                <div className="flex flex-col items-center gap-2">
                  <button
                    type="button"
                    className={`h-20 w-20 touch-none select-none rounded-full border-4 text-sm font-bold transition-colors ${
                      ui.engaged ? 'border-black/20 bg-[#f0b400] text-black' : 'border-zinc-300 bg-white text-zinc-800 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-100'
                    }`}
                    onPointerDown={(e) => {
                      try {
                        e.currentTarget.setPointerCapture(e.pointerId);
                      } catch {
                        /* synthetic pointer */
                      }
                      clutchBtn.current = true;
                    }}
                    onPointerUp={() => (clutchBtn.current = false)}
                    onPointerCancel={() => (clutchBtn.current = false)}
                    onLostPointerCapture={() => (clutchBtn.current = false)}
                    onContextMenu={(e) => e.preventDefault()}
                    data-testid="tdm59-clutch-button"
                  >
                    Hold to
                    <br />
                    drive
                  </button>
                  <GripperSlider value={gripper} onChange={setGripper} compact />
                </div>
                <Tdm56Joystick label="Lift and turn" hint="up, down, turn" onChange={(v) => (stickR.current = v)} size={stickSize} />
              </div>
              <label className="mt-2 flex items-center justify-center gap-2 text-xs text-zinc-600 dark:text-zinc-400">
                <input type="checkbox" checked={stickEngages} onChange={(e) => setStickEngages(e.target.checked)} />
                Touching a stick holds the clutch
              </label>
            </div>
          )}
        </div>

        {/* ---------------- side panel */}
        <aside className="flex flex-col gap-4 text-sm">
          <Panel title="Readiness check" aside={<span className="tabular-nums text-zinc-500">{lessonsDone + (netOk ? 1 : 0)}/{LESSONS.length + 1}</span>}>
            <ol className="space-y-1" data-testid="tdm59-lessons">
              {LESSONS.map((l, i) => (
                <li key={l.id} className={`flex items-center gap-2 rounded-md px-1.5 py-1 ${current?.id === l.id ? 'bg-amber-50 dark:bg-amber-950/30' : ''}`} data-done={done[l.id]}>
                  <Check on={done[l.id]} n={i + 1} />
                  <span className={done[l.id] ? 'text-zinc-500 line-through decoration-zinc-400/60' : current?.id === l.id ? 'font-semibold' : ''}>{l.title}</span>
                </li>
              ))}
              <li className="flex items-start gap-2 rounded-md px-1.5 py-1" data-done={netOk}>
                <Check on={netOk} n={LESSONS.length + 1} />
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
              <p className="mt-2 text-xs text-zinc-500">Finish every step to unlock claiming paid teleop tasks.</p>
            )}
          </Panel>

          <Panel title="Speed">
            <div className="flex items-baseline justify-between">
              <span className="text-2xl font-semibold tabular-nums">{Math.round(effSpeed * 100)}%</span>
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={slow} onChange={(e) => setSlow(e.target.checked)} />
                Slow mode
              </label>
            </div>
            <input type="range" min={0.1} max={1} step={0.05} value={speed} onChange={(e) => setSpeed(Number(e.target.value))} className="mt-2 w-full accent-zinc-700 dark:accent-zinc-300" aria-label="Speed scale" />
            <p className="mt-1 text-xs text-zinc-500">{slow ? `Slow mode caps speed at ${SLOW_CAP * 100}%.` : 'Full range. The real robot still applies its own limits.'}</p>
          </Panel>

          <Panel title="Gripper">
            <GripperSlider value={gripper} onChange={setGripper} />
            <p className="mt-1 text-xs text-zinc-500">The gripper moves while you hold the clutch.</p>
          </Panel>

          <Panel title="Feel the lag" aside={<LatencyBadge videoMs={lag} cmdHz={ui.hz} driving={ui.engaged} simulated pauseAtMs={PAUSE_AT_MS} />}>
            <div className="flex gap-1" role="group" aria-label="Simulated video delay">
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
              Adds a delay to everything you send, like a slow network. Under 150 ms feels smooth; over 300 ms is too laggy to drive well; over 400 ms the real page pauses driving.
            </p>
          </Panel>

          <Panel title="Controls">
            <p className="mb-2 text-xs text-zinc-500">{ui.pad ? `Gamepad connected: ${ui.pad}` : 'No gamepad found. Press any button on one to connect it.'}</p>
            <KeyTable
              rows={[
                ['Drive (clutch)', 'Hold Space', 'Hold RT'],
                ['Forward / back', 'W / S', 'Left stick'],
                ['Left / right', 'A / D', 'Left stick'],
                ['Up / down', 'Q / E', 'Right stick up/down'],
                ['Turn', '← / →', 'Right stick left/right'],
                ['Tilt', '↑ / ↓', 'D-pad up/down'],
                ['Gripper', 'R open, F close', 'A toggles, LB/RB'],
                ['Stop', 'Esc', 'B'],
              ]}
            />
          </Panel>
        </aside>
      </div>

      <InstructionsOverlay open={intro} onClose={closeIntro} kind="practice" taskText={taskText ?? 'Pick up the blue block and drop it in the bin'} doneLabel="Start practising" />
    </div>
  );
}

// ------------------------------------------------------------------ bits

function SimFrame({ label, lagMs, compact, children }: { label: string; lagMs: number; compact?: boolean; children: ReactNode }) {
  const tone = lagMs < 150 ? 'bg-emerald-600/85 text-white' : lagMs <= 300 ? 'bg-amber-500/90 text-black' : 'bg-red-600/90 text-white';
  return (
    <div className="relative overflow-hidden rounded-lg bg-[#1d2630]" style={{ aspectRatio: String(SIM_VIEW_ASPECT) }}>
      {children}
      <span className={`pointer-events-none absolute left-2 top-2 rounded-md bg-black/55 px-1.5 py-0.5 font-medium text-white ${compact ? 'text-[10px] sm:text-xs' : 'text-xs'}`}>{label}</span>
      <span className={`pointer-events-none absolute right-2 top-2 rounded-md px-1.5 py-0.5 text-[10px] font-semibold tabular-nums sm:text-xs ${tone} ${compact ? 'hidden sm:inline' : ''}`}>
        {lagMs} ms
      </span>
    </div>
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
    <section className="rounded-xl border border-zinc-200 bg-white p-3 dark:border-zinc-800 dark:bg-zinc-900">
      <div className="mb-2 flex items-center justify-between gap-2">
        <h2 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">{title}</h2>
        {aside}
      </div>
      {children}
    </section>
  );
}

function GripperSlider({ value, onChange, compact }: { value: number; onChange: (v: number) => void; compact?: boolean }) {
  return (
    <div className={compact ? 'w-28' : 'w-full'}>
      <input
        type="range"
        min={0}
        max={1}
        step={0.01}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="w-full touch-none accent-[#f0b400]"
        aria-label="Gripper opening"
        data-testid={compact ? 'tdm59-gripper-compact' : 'tdm59-gripper'}
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
