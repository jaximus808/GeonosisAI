'use client';

// TDM-59: where the operator is in a teleop session, in five plain states.
//   connecting -> viewing -> controlling -> recording -> uploading
// The states come from lib/teleop/tdm59Telemetry.ts (TDM-56 console + TDM-58 recording markup).

import type { SessionState } from '@/lib/teleop/tdm59Telemetry';

const STATES: { id: SessionState; label: string; hint: string }[] = [
  { id: 'connecting', label: 'Connecting', hint: 'Reaching the robot and its cameras.' },
  { id: 'viewing', label: 'Watching', hint: 'You can see the robot. Hold the clutch to drive.' },
  { id: 'controlling', label: 'Driving', hint: 'You are moving the arm. Let go to hold still.' },
  { id: 'recording', label: 'Recording', hint: 'Recording your demo. Finish the job, then stop recording.' },
  { id: 'uploading', label: 'Uploading', hint: 'Sending your demo to the factory. Keep this page open.' },
];

/** TDM-86 phone cockpit: the strip as one word ("Watching"), for the top bar. */
export function sessionStateLabel(state: SessionState): string {
  return (STATES.find((s) => s.id === state) ?? STATES[0]).label;
}

export function SessionStateBar({ state, className = '' }: { state: SessionState; className?: string }) {
  const cur = STATES.findIndex((s) => s.id === state);
  const info = STATES[Math.max(0, cur)];
  return (
    <div className={className} data-testid="tdm59-session" data-state={state}>
      <ol className="flex gap-1" aria-label="Session progress">
        {STATES.map((s, i) => (
          <li
            key={s.id}
            aria-current={i === cur ? 'step' : undefined}
            className={`min-w-0 flex-1 truncate whitespace-nowrap rounded-md px-1 py-1 text-center text-[11px] font-medium transition-colors sm:px-2.5 sm:text-xs ${
              i === cur
                ? s.id === 'recording'
                  ? 'bg-red-600 text-white'
                  : s.id === 'controlling'
                    ? 'bg-[#f0b400] text-black'
                    : 'bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900'
                : i < cur
                  ? 'bg-zinc-200 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400'
                  : 'bg-zinc-100 text-zinc-400 dark:bg-zinc-900 dark:text-zinc-500'
            }`}
          >
            {s.id === 'recording' && i === cur && <span className="mr-1 inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-white align-middle" />}
            {s.label}
          </li>
        ))}
      </ol>
      <p className="mt-1 truncate text-xs text-zinc-500" aria-live="polite" title={info.hint}>
        {info.hint}
      </p>
    </div>
  );
}
