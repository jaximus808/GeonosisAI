'use client';

// TDM-59: the short "how to drive" walkthrough for first-time operators.
//
// Six cards: move, up/down/turn, gripper, speed, STOP, and what a good demo looks like for
// THIS task. The keycaps light up as you press them (keyboard) so it is learn-by-doing, without
// moving anything: while the overlay is open on the teleop page, <Tdm59TeleopOnboarding> swallows
// key presses before the TDM-56 console sees them (see `useTdm59KeyShield`).
//
//   <InstructionsOverlay open onClose kind="clean_teleop" taskText="Put the cup in the bin" practiceHref="/practice" />

import Link from 'next/link';
import { useEffect, useRef, useState, type ReactNode } from 'react';

export type OverlayKind = 'clean_teleop' | 'recovery_takeover' | 'practice';

export interface InstructionsOverlayProps {
  open: boolean;
  onClose: () => void;
  kind: OverlayKind;
  /** The ticket's job in plain words, e.g. "Put the cup in the bin". */
  taskText?: string | null;
  /** Shown on the last card when the operator could practise first. */
  practiceHref?: string;
  /** Label of the final button. */
  doneLabel?: string;
  startAt?: number;
}

type Input = 'keyboard' | 'touch';

/** keydown codes that a key shield (Tdm59TeleopOnboarding) swallowed, re-sent as a CustomEvent<string>. */
export const TDM59_SHIELDED_KEY = 'tdm59-shielded-key';

/** Native keydown, or the shield's re-sent copy when the native one was swallowed. */
function onKeyDownCode(cb: (code: string) => void) {
  const native = (e: KeyboardEvent) => cb(e.code);
  const shielded = (e: Event) => cb((e as CustomEvent<string>).detail);
  window.addEventListener('keydown', native);
  window.addEventListener(TDM59_SHIELDED_KEY, shielded);
  return () => {
    window.removeEventListener('keydown', native);
    window.removeEventListener(TDM59_SHIELDED_KEY, shielded);
  };
}

function useHeldKeys(active: boolean) {
  const [held, setHeld] = useState<Set<string>>(new Set());
  useEffect(() => {
    if (!active) return;
    const offDown = onKeyDownCode((code) => setHeld((h) => (h.has(code) ? h : new Set(h).add(code))));
    const up = (e: KeyboardEvent) =>
      setHeld((h) => {
        if (!h.has(e.code)) return h;
        const n = new Set(h);
        n.delete(e.code);
        return n;
      });
    const blur = () => setHeld(new Set());
    window.addEventListener('keyup', up);
    window.addEventListener('blur', blur);
    return () => {
      offDown();
      window.removeEventListener('keyup', up);
      window.removeEventListener('blur', blur);
    };
  }, [active]);
  return held;
}

export function InstructionsOverlay({ open, onClose, kind, taskText, practiceHref, doneLabel, startAt = 0 }: InstructionsOverlayProps) {
  const [i, setI] = useState(startAt);
  const [input, setInput] = useState<Input>('keyboard');
  const held = useHeldKeys(open);
  const panel = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (open) setI(startAt);
  }, [open, startAt]);
  useEffect(() => {
    setInput(window.matchMedia?.('(pointer: coarse)').matches ? 'touch' : 'keyboard');
  }, []);
  useEffect(() => {
    if (!open) return;
    panel.current?.focus();
    return onKeyDownCode((code) => code === 'Escape' && onClose());
  }, [open, onClose]);

  if (!open) return null;
  const cards = buildCards(kind, taskText, input, held);
  const card = cards[i];
  const last = i === cards.length - 1;

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-zinc-950/60 backdrop-blur-sm sm:items-center sm:p-6" role="presentation">
      <div
        ref={panel}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby="tdm59-intro-title"
        className="flex max-h-[92dvh] w-full max-w-xl flex-col overflow-hidden rounded-t-3xl bg-white shadow-2xl outline-none dark:bg-zinc-900 sm:rounded-3xl"
        data-testid="tdm59-intro"
        data-step={i}
      >
        <div className="flex items-center gap-3 px-5 pt-4">
          <ol className="flex flex-1 gap-1.5" aria-label={`Step ${i + 1} of ${cards.length}`}>
            {cards.map((c, k) => (
              <li key={c.title} className="flex-1">
                <button
                  type="button"
                  onClick={() => setI(k)}
                  className={`block h-1.5 w-full rounded-full transition-colors ${k <= i ? 'bg-[#f0b400]' : 'bg-zinc-200 dark:bg-zinc-700'}`}
                  aria-label={`Go to step ${k + 1}: ${c.title}`}
                />
              </li>
            ))}
          </ol>
          <div className="flex rounded-full bg-zinc-100 p-0.5 text-xs dark:bg-zinc-800" role="group" aria-label="Show controls for">
            {(['keyboard', 'touch'] as const).map((m) => (
              <button
                key={m}
                type="button"
                onClick={() => setInput(m)}
                aria-pressed={input === m}
                className={`rounded-full px-2.5 py-1 ${input === m ? 'bg-white font-semibold shadow-sm dark:bg-zinc-600' : 'text-zinc-500'}`}
              >
                {m === 'keyboard' ? 'Keyboard' : 'Phone'}
              </button>
            ))}
          </div>
          <button type="button" onClick={onClose} className="rounded-full p-1.5 text-zinc-500 hover:bg-zinc-100 dark:hover:bg-zinc-800" aria-label="Close instructions">
            <svg viewBox="0 0 20 20" className="h-5 w-5" aria-hidden>
              <path d="M5 5l10 10M15 5L5 15" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
            </svg>
          </button>
        </div>

        <div className="overflow-y-auto px-5 pb-2 pt-4">
          <div className="mb-4 flex min-h-[148px] items-center justify-center rounded-2xl bg-zinc-100 px-4 py-5 dark:bg-zinc-950" aria-hidden>
            {card.diagram}
          </div>
          <h2 id="tdm59-intro-title" className="text-xl font-semibold tracking-tight">
            {card.title}
          </h2>
          <div className="mt-2 space-y-2 text-[15px] leading-relaxed text-zinc-700 dark:text-zinc-300">{card.body}</div>
          {card.tryIt && input === 'keyboard' && (
            <p className="mt-3 text-sm text-zinc-500">
              Try it: {card.tryIt}. Nothing moves while this is open.
            </p>
          )}
        </div>

        <div className="flex items-center gap-2 border-t border-zinc-100 px-5 py-3 dark:border-zinc-800">
          {i > 0 ? (
            <button type="button" onClick={() => setI(i - 1)} className="rounded-xl px-4 py-2.5 text-sm font-medium text-zinc-600 hover:bg-zinc-100 dark:text-zinc-300 dark:hover:bg-zinc-800">
              Back
            </button>
          ) : (
            <button type="button" onClick={onClose} className="rounded-xl px-4 py-2.5 text-sm font-medium text-zinc-500 hover:bg-zinc-100 dark:hover:bg-zinc-800">
              Skip
            </button>
          )}
          {last && practiceHref && (
            <Link href={practiceHref} className="ml-auto rounded-xl px-3 py-2.5 text-sm font-medium text-zinc-700 underline-offset-2 hover:underline dark:text-zinc-300">
              Practise on a simulated arm
            </Link>
          )}
          <button
            type="button"
            onClick={() => (last ? onClose() : setI(i + 1))}
            className={`${last && practiceHref ? '' : 'ml-auto'} rounded-xl bg-zinc-900 px-5 py-2.5 text-sm font-semibold text-white hover:bg-zinc-700 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-white`}
            data-testid="tdm59-intro-next"
          >
            {last ? (doneLabel ?? 'Start driving') : 'Next'}
          </button>
        </div>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ content

interface Card {
  title: string;
  body: ReactNode;
  diagram: ReactNode;
  tryIt?: string;
}

function buildCards(kind: OverlayKind, taskText: string | null | undefined, input: Input, held: Set<string>): Card[] {
  const k = (c: string) => held.has(c);
  const touch = input === 'touch';
  const job = taskText?.trim();
  const takeover = kind === 'recovery_takeover';

  return [
    {
      title: 'Hold a key to move',
      diagram: touch ? (
        <TouchLayout lit="sticks" />
      ) : (
        <div className="flex items-center gap-5">
          <KeyCluster label="Move" keys={[['', 'KeyW', ''], ['KeyA', 'KeyS', 'KeyD']]} held={held} />
          <ArmState moving={['KeyW', 'KeyA', 'KeyS', 'KeyD', 'KeyQ', 'KeyE'].some(k)} />
        </div>
      ),
      body: (
        <>
          <p>
            {touch ? (
              <>
                Drag the <b>Move</b> stick. The arm moves only while your thumb is on a stick.
              </>
            ) : (
              <>
                Hold <b>W A S D</b> to move the hand forward, left, back and right. The arm moves only while you hold a key.
              </>
            )}
          </p>
          <p>Let go and the arm holds still where it is. Let go any time you are unsure, then line up your next move.</p>
        </>
      ),
      tryIt: 'hold W',
    },
    {
      title: 'Up, down and turning',
      diagram: touch ? (
        <TouchLayout lit="sticks" />
      ) : (
        <div className="flex flex-wrap items-end justify-center gap-6">
          <KeyCluster label="Up / down" keys={[['KeyQ'], ['KeyE']]} held={held} />
          <KeyCluster label="Turn (when on)" keys={[['', 'ArrowUp', ''], ['ArrowLeft', 'ArrowDown', 'ArrowRight']]} held={held} />
        </div>
      ),
      body: touch ? (
        <>
          <p>Drag the <b>Up / down</b> stick to lift or lower the hand.</p>
          <p>The hand keeps its angle unless you switch on <b>Turn the hand</b>. Most pick-and-place jobs never need it.</p>
        </>
      ) : (
        <>
          <p>
            <b>Q</b> lifts the hand, <b>E</b> lowers it. The hand keeps its angle unless you switch on <b>Turn the hand</b>; then the arrow keys turn and tilt it.
          </p>
          <p>Short moves are easier to control than long ones. Hold <b>Shift</b> for fine moves.</p>
        </>
      ),
      tryIt: 'press Q or E',
    },
    {
      title: 'Open and close the gripper',
      diagram: touch ? (
        <GripperDiagram open={0.7} />
      ) : (
        <div className="flex items-center gap-6">
          <div className="flex flex-col items-center gap-1.5">
            <Key on={k('KeyG')}>G</Key>
            <span className="text-xs text-zinc-500">open / close</span>
          </div>
          <GripperDiagram open={k('KeyG') ? 0.15 : 0.9} />
        </div>
      ),
      body: touch ? (
        <p>Tap <b>Close grip</b> to grab and <b>Open grip</b> to let go. The arm holds still while the fingers move.</p>
      ) : (
        <p>
          Tap <b>G</b> to close the gripper, tap it again to open it. The arm holds still while the fingers move. On a gamepad, <b>A</b> does the same.
        </p>
      ),
      tryIt: 'tap G',
    },
    {
      title: 'Start slow',
      diagram: <SpeedDiagram />,
      body: (
        <>
          <p>You start at 35% speed, and every move eases in, so a quick tap is a small move. Pick Fast once the hand goes where you mean it to.</p>
          <p>The robot keeps its own speed and workspace limits whatever you set here. If the picture lags, slow down rather than speed up.</p>
        </>
      ),
    },
    {
      title: 'STOP is always there',
      diagram: (
        <div className="flex items-center gap-5">
          <span className="rounded-2xl bg-[#d4221a] px-7 py-4 text-2xl font-black tracking-wide text-white shadow-[0_4px_0_#7f1410]">STOP</span>
          {!touch && <Key on={k('Escape')}>Esc</Key>}
        </div>
      ),
      body: (
        <>
          <p>
            The red STOP button at the top of the page stops the arm at once{touch ? '' : <>. So does <b>Esc</b>, or <b>B</b> on a gamepad</>}. It stays stopped until you choose to resume.
          </p>
          <p>Use it whenever something looks wrong. Nobody is charged for a stop. There is also a physical e-stop next to the robot.</p>
        </>
      ),
    },
    {
      title: takeover ? 'What a good takeover looks like' : 'What a good demo looks like',
      diagram: <DemoTimeline takeover={takeover} />,
      body: (
        <>
          {job && (
            <p className="rounded-xl bg-amber-50 px-3 py-2 text-zinc-900 ring-1 ring-amber-200 dark:bg-amber-950/40 dark:text-amber-50 dark:ring-amber-900">
              The job: <b>{job}</b>
            </p>
          )}
          <ul className="list-disc space-y-1 pl-5">
            {takeover ? (
              <>
                <li>The robot stopped partway. Take over and finish the job from where it is.</li>
                <li>One steady attempt, no long pauses, then put the hand somewhere safe and tap Hand back.</li>
                <li>You are paid once the reviewer sees the job finished smoothly.</li>
              </>
            ) : (
              <>
                <li>Start with the arm still and the gripper open, then press Record.</li>
                <li>Do the whole job once, smoothly and without long pauses. Steady beats fast.</li>
                <li>Finish with the object in place and the hand clear, then stop recording and say whether it worked.</li>
                <li>Keep it between 2 seconds and 2 minutes. Fumbled it? Discard and record again instead of fixing it mid-take.</li>
              </>
            )}
          </ul>
        </>
      ),
    },
  ];
}

// ------------------------------------------------------------------ diagrams

const KEY_LABEL: Record<string, string> = {
  KeyW: 'W',
  KeyA: 'A',
  KeyS: 'S',
  KeyD: 'D',
  KeyQ: 'Q',
  KeyE: 'E',
  ArrowUp: '↑',
  ArrowDown: '↓',
  ArrowLeft: '←',
  ArrowRight: '→',
};

function Key({ children, on, wide }: { children: ReactNode; on?: boolean; wide?: boolean }) {
  return (
    <kbd
      className={`inline-flex h-11 items-center justify-center rounded-lg border-b-4 font-sans text-base font-semibold transition-colors ${wide ? 'w-40' : 'w-11'} ${
        on
          ? 'translate-y-0.5 border-[#a67c00] bg-[#f0b400] text-black'
          : 'border-zinc-300 bg-white text-zinc-700 dark:border-zinc-600 dark:bg-zinc-800 dark:text-zinc-200'
      }`}
    >
      {children}
    </kbd>
  );
}

function KeyCluster({ label, keys, held }: { label: string; keys: string[][]; held: Set<string> }) {
  return (
    <div className="flex flex-col items-center gap-1">
      {keys.map((row, r) => (
        <div key={r} className="flex gap-1">
          {row.map((c, j) => (c ? <Key key={c} on={held.has(c)}>{KEY_LABEL[c] ?? c}</Key> : <span key={`gap${j}`} className="w-11" />))}
        </div>
      ))}
      <span className="mt-1 text-xs text-zinc-500">{label}</span>
    </div>
  );
}

function ArmState({ moving }: { moving: boolean }) {
  return (
    <svg viewBox="0 0 120 90" className="h-24 w-32">
      <rect x="10" y="78" width="100" height="4" rx="2" className="fill-zinc-300 dark:fill-zinc-700" />
      <rect x="22" y="58" width="16" height="20" rx="3" className="fill-zinc-500" />
      <g style={{ transform: moving ? 'rotate(-14deg)' : 'rotate(0deg)', transformOrigin: '30px 58px', transition: 'transform 400ms ease' }}>
        <path d="M30 58 L62 26 L94 44" fill="none" strokeWidth="8" strokeLinecap="round" strokeLinejoin="round" className={moving ? 'stroke-[#f0b400]' : 'stroke-zinc-400'} />
        <path d="M94 44 l6 10 M94 44 l-4 11" strokeWidth="4" strokeLinecap="round" className="stroke-zinc-500" />
      </g>
      <text x="60" y="12" textAnchor="middle" className={`text-[11px] font-semibold ${moving ? 'fill-[#a67c00]' : 'fill-zinc-500'}`}>
        {moving ? 'Driving' : 'Holding still'}
      </text>
    </svg>
  );
}

function TouchLayout({ lit }: { lit: 'clutch' | 'sticks' }) {
  const stick = (cx: number, label: string) => (
    <g>
      <circle cx={cx} cy="48" r="30" strokeWidth="3" className={lit === 'sticks' ? 'fill-[#f0b400]/15 stroke-[#f0b400]' : 'fill-zinc-200 stroke-zinc-300 dark:fill-zinc-800 dark:stroke-zinc-700'} />
      <circle cx={cx} cy="48" r="11" className={lit === 'sticks' ? 'fill-[#f0b400]' : 'fill-zinc-400'} />
      <text x={cx} y="96" textAnchor="middle" className="fill-zinc-500 text-[10px]">
        {label}
      </text>
    </g>
  );
  return (
    <svg viewBox="0 0 260 104" className="h-32 w-full max-w-xs">
      {stick(40, 'Move')}
      <circle cx="130" cy="44" r="26" strokeWidth="3" className={lit === 'clutch' ? 'fill-[#f0b400] stroke-[#a67c00]' : 'fill-white stroke-zinc-300 dark:fill-zinc-900 dark:stroke-zinc-700'} />
      <text x="130" y="41" textAnchor="middle" className="fill-zinc-900 text-[9px] font-bold dark:fill-zinc-100">
        Hold to
      </text>
      <text x="130" y="52" textAnchor="middle" className="fill-zinc-900 text-[9px] font-bold dark:fill-zinc-100">
        drive
      </text>
      <rect x="104" y="82" width="52" height="5" rx="2.5" className="fill-zinc-300 dark:fill-zinc-700" />
      <circle cx="138" cy="84.5" r="6" className="fill-[#f0b400]" />
      <text x="130" y="100" textAnchor="middle" className="fill-zinc-500 text-[10px]">
        Gripper
      </text>
      {stick(220, 'Lift and turn')}
    </svg>
  );
}

function GripperDiagram({ open }: { open: number }) {
  const gap = 6 + open * 30;
  return (
    <svg viewBox="0 0 100 90" className="h-24 w-24">
      <rect x="40" y="6" width="20" height="22" rx="3" className="fill-zinc-500" />
      <rect x="22" y="28" width="56" height="8" rx="2" className="fill-zinc-500" />
      <rect x={50 - gap / 2 - 8} y="36" width="8" height="40" rx="2" className="fill-zinc-400" style={{ transition: 'x 200ms ease' }} />
      <rect x={50 + gap / 2} y="36" width="8" height="40" rx="2" className="fill-zinc-400" style={{ transition: 'x 200ms ease' }} />
      <rect x="40" y="60" width="20" height="20" rx="2" className="fill-[#2f6fde]/80" />
    </svg>
  );
}

function SpeedDiagram() {
  return (
    <svg viewBox="0 0 220 70" className="h-20 w-full max-w-xs">
      <rect x="10" y="30" width="200" height="8" rx="4" className="fill-zinc-300 dark:fill-zinc-700" />
      <rect x="10" y="30" width="70" height="8" rx="4" className="fill-[#f0b400]" />
      <circle cx="80" cy="34" r="10" className="fill-white stroke-[#a67c00] dark:fill-zinc-900" strokeWidth="3" />
      <line x1="80" y1="12" x2="80" y2="20" className="stroke-zinc-500" strokeWidth="2" />
      <text x="80" y="9" textAnchor="middle" className="fill-zinc-600 text-[10px] font-semibold dark:fill-zinc-300">
        start 35%
      </text>
      <text x="10" y="60" className="fill-zinc-500 text-[10px]">
        careful
      </text>
      <text x="210" y="60" textAnchor="end" className="fill-zinc-500 text-[10px]">
        full speed
      </text>
    </svg>
  );
}

function DemoTimeline({ takeover }: { takeover: boolean }) {
  const steps = takeover ? ['Robot stuck', 'Take over', 'Finish the job', 'Hand back'] : ['Arm still', 'Record', 'One smooth run', 'Stop + say if it worked'];
  return (
    <ol className="flex w-full max-w-md items-start justify-between gap-1 text-center text-[11px] leading-tight text-zinc-600 dark:text-zinc-300">
      {steps.map((s, i) => (
        <li key={s} className="relative flex flex-1 flex-col items-center gap-1.5">
          {i > 0 && <span className="absolute right-1/2 top-3 h-0.5 w-full -translate-y-1/2 bg-zinc-300 dark:bg-zinc-700" aria-hidden />}
          <span
            className={`relative z-10 flex h-6 w-6 items-center justify-center rounded-full text-xs font-bold ${
              i === 2 ? 'bg-[#f0b400] text-black' : 'bg-white text-zinc-700 ring-2 ring-zinc-300 dark:bg-zinc-900 dark:text-zinc-200 dark:ring-zinc-700'
            }`}
          >
            {i + 1}
          </span>
          <span>{s}</span>
        </li>
      ))}
    </ol>
  );
}
