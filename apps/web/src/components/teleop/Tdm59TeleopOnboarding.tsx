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
import { useTdm86Phone } from '@/components/teleop/Tdm86Phone';
import { useRows } from '@/lib/spacetime/hooks';
import { cleanInstructions } from '@/lib/marketplace/tdm61';
import { introSeen, markIntroSeen, useTdm59Readiness } from '@/lib/teleop/tdm59Readiness';
import { useConsoleTelemetry } from '@/lib/teleop/tdm59Telemetry';

/** The phone cockpit's "?" button fires this on window: open the "How to drive" walkthrough. */
export const TDM59_OPEN_HELP = 'tdm59:open-help';

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
  const { status } = useTdm59Readiness();
  useEffect(() => {
    // TDM-86: someone who passed practice already learnt the controls; don't put a walkthrough over
    // the arm they just claimed (it stays one tap away under "How to drive")
    if (status === 'not_ready' && !introSeen(kind)) setOpen(true);
  }, [kind, status]);
  const close = useCallback(() => {
    markIntroSeen(kind);
    setOpen(false);
  }, [kind]);

  useEffect(() => {
    const show = () => setOpen(true);
    window.addEventListener(TDM59_OPEN_HELP, show);
    return () => window.removeEventListener(TDM59_OPEN_HELP, show);
  }, []);

  const t = useConsoleTelemetry();
  const driving = t.clutch === 'drive';
  // TDM-86: on a phone the console's full-screen cockpit carries the connection chip, the session
  // state (one word in its top bar) and a "?" that opens this walkthrough; only the dialog lives here
  const { phone } = useTdm86Phone();
  if (phone) return <InstructionsOverlay open={open} onClose={close} kind={kind} taskText={taskText} />;

  return (
    <div className="mb-3 flex flex-col gap-2" data-testid="tdm59-onboarding">
      <SessionStateBar state={t.session} className="max-w-2xl" />
      {/* one fixed-height row: a bad-connection tip or the practice link never pushes the console down */}
      <div className="flex h-9 min-w-0 flex-nowrap items-center gap-2 overflow-x-clip">
        <LatencyBadge
          className="min-w-0 shrink"
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
          className="shrink-0 rounded-full px-3 py-1.5 text-sm font-medium text-zinc-700 ring-1 ring-zinc-300 hover:bg-zinc-100 dark:text-zinc-200 dark:ring-zinc-700 dark:hover:bg-zinc-800"
          data-testid="tdm59-help"
        >
          How to drive
        </button>
        {status === 'not_ready' && (
          <Link
            href="/practice"
            target="_blank"
            className="shrink-0 whitespace-nowrap rounded-full px-3 py-1.5 text-sm text-zinc-600 underline-offset-2 hover:underline dark:text-zinc-400"
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
