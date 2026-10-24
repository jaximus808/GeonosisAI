'use client';

// TDM-59: first-time-operator layer for /teleop/[taskId]. One mount line, placed BEFORE the
// TDM-56 console so its key shield is registered first:
//
//   <Tdm59TeleopOnboarding taskId={taskId} />
//   <Tdm56TeleopConsole ... />
//
// It adds, without touching the console:
//   - the "how to drive" walkthrough, opened automatically on the first visit per task type
//     (remembered in localStorage) and on demand from the "How to drive" button
//   - the session state strip: connecting / watching / driving / recording / uploading
//   - a connection indicator graded against clear thresholds, with advice ("Too laggy. Try Wi-Fi.")
//   - a link to practice mode for operators who have not passed the readiness check
//
// While the walkthrough is open, key presses are swallowed before the console's window listener
// sees them, so trying keys there never moves the arm. Gamepad input is polled by the console and
// is not blocked; the walkthrough is dismissed before anyone reaches for a pad in practice.

import Link from 'next/link';
import { useCallback, useEffect, useRef, useState } from 'react';
import { InstructionsOverlay, TDM59_SHIELDED_KEY, type OverlayKind } from '@/components/teleop/InstructionsOverlay';
import { LatencyBadge } from '@/components/teleop/LatencyBadge';
import { SessionStateBar } from '@/components/teleop/SessionStateBar';
import { useRows } from '@/lib/spacetime/hooks';
import { cleanInstructions } from '@/lib/marketplace/tdm61';
import { introSeen, markIntroSeen, useTdm59Readiness } from '@/lib/teleop/tdm59Readiness';
import { useConsoleTelemetry } from '@/lib/teleop/tdm59Telemetry';

/** Swallow keydown on window (capture) while `active`. Must be registered before the console's listener. */
export function useTdm59KeyShield(active: boolean) {
  const on = useRef(active);
  on.current = active;
  useEffect(() => {
    const down = (e: KeyboardEvent) => {
      if (!on.current) return;
      const t = e.target as HTMLElement | null;
      // Never let a key reach the console. Default actions (Tab focus, Enter/Space on the dialog's
      // buttons) still happen because nothing here calls preventDefault on them.
      e.stopImmediatePropagation();
      if (e.code === 'Space' && t?.tagName !== 'BUTTON') e.preventDefault();
      // That also hides the key from the overlay's own window listeners, so hand it over directly.
      window.dispatchEvent(new CustomEvent(TDM59_SHIELDED_KEY, { detail: e.code }));
    };
    window.addEventListener('keydown', down, true);
    return () => window.removeEventListener('keydown', down, true);
  }, []);
}

export function Tdm59TeleopOnboarding({ taskId }: { taskId: string }) {
  const id = /^\d+$/.test(taskId) ? BigInt(taskId) : 0n;
  const [tasks] = useRows('task', (r) => r.id.eq(id));
  const task = tasks[0];
  const [tickets] = useRows('ticket', (r) => r.id.eq(task?.ticketId ?? 0n));
  const ticket = tickets[0];
  const kind: OverlayKind = task?.type === 'recovery_takeover' ? 'recovery_takeover' : 'clean_teleop';
  const taskText = ticket?.taskDescription || (task ? cleanInstructions(task.instructions) : null);

  const [open, setOpen] = useState(false);
  useTdm59KeyShield(open);
  useEffect(() => {
    if (!introSeen(kind)) setOpen(true);
  }, [kind]);
  const close = useCallback(() => {
    markIntroSeen(kind);
    setOpen(false);
  }, [kind]);

  const t = useConsoleTelemetry();
  const { status } = useTdm59Readiness();
  const driving = t.clutch === 'drive';

  return (
    <div className="mb-3 flex flex-col gap-2" data-testid="tdm59-onboarding">
      <SessionStateBar state={t.session} className="max-w-2xl" />
      <div className="flex flex-wrap items-start gap-2">
        <LatencyBadge
          videoMs={t.feed?.worstLatencyMs}
          videoStale={t.feed ? t.feed.stale && (t.feed.state === 'live' || t.feed.state === 'fallback') : false}
          videoDown={t.feed && ['denied', 'error', 'closed'].includes(t.feed.state) ? t.feed.state : undefined}
          rttMs={t.rttMs}
          cmdHz={t.cmdHz}
          driving={driving}
        />
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="rounded-full px-3 py-1.5 text-sm font-medium text-zinc-700 ring-1 ring-zinc-300 hover:bg-zinc-100 dark:text-zinc-200 dark:ring-zinc-700 dark:hover:bg-zinc-800"
          data-testid="tdm59-help"
        >
          How to drive
        </button>
        {status === 'not_ready' && (
          <Link
            href="/practice"
            target="_blank"
            className="rounded-full px-3 py-1.5 text-sm text-zinc-600 underline-offset-2 hover:underline dark:text-zinc-400"
            title="Opens in a new tab. Your claim stays held while this tab stays open."
          >
            Practise first
          </Link>
        )}
      </div>
      <InstructionsOverlay open={open} onClose={close} kind={kind} taskText={taskText} />
    </div>
  );
}
