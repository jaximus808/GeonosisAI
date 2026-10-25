'use client';

// Reset arm + Exit on the teleop page, next to the recording controls (TeleopSession puts it in the console's
// recordingSlot). Logic: lib/teleop/armSession.ts.
//
//   Reset arm  {t:"reset_arm"}: the robot ramps slowly back to its rest pose (LARP's home on the real YAM) and
//              opens the gripper. Shows "Resetting… 40%". Disabled while recording. STOP still aborts it.
//   Exit       recording? "Discard this recording?" (or Finish and exit) -> reset the arm and wait -> give back
//              control -> the task list. If the reset fails: say why, offer "Leave anyway".
//
// Closing the tab or losing the claim never moves the arm (the robot holds): only these two buttons do.

import { useCallback, useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import { useTdm56Teleop } from '@/components/teleop/Tdm56TeleopConsole';
import {
  leave,
  recordingActive,
  requestReset,
  resetErrorText,
  resetLabel,
  resetProgress,
  runExit,
  type ExitStep,
} from '@/lib/teleop/armSession';

type Ui =
  | { k: 'idle' }
  | { k: 'ask_recording' }
  | { k: 'exiting'; step: ExitStep }
  | { k: 'failed'; text: string; exiting: boolean };

export function ArmSessionButtons({ release, compact }: { release?: () => Promise<void>; compact?: boolean }) {
  const { controlSocket, robot, link } = useTdm56Teleop();
  const router = useRouter();
  const [ui, setUi] = useState<Ui>({ k: 'idle' });
  const [resetBusy, setResetBusy] = useState(false);
  const alive = useRef(true);
  useEffect(() => () => void (alive.current = false), []);

  const prog = resetProgress(robot?.raw);
  const recording = recordingActive(robot?.recording ?? robot?.raw?.recording);
  const open = link.state === 'open';
  const stopped = robot?.stopped === true;
  const exiting = ui.k === 'exiting';
  const doRelease = useCallback(() => (release ? release() : Promise.resolve()), [release]);
  const navigate = useCallback((href: string) => router.push(href), [router]);

  const onReset = useCallback(async () => {
    setResetBusy(true);
    const r = await requestReset(controlSocket);
    if (!alive.current) return;
    setResetBusy(false);
    if (!r.ok) setUi({ k: 'failed', text: resetErrorText(r), exiting: false });
  }, [controlSocket]);

  const exit = useCallback(
    async (rec: 'discard' | 'finish' | null) => {
      setUi({ k: 'exiting', step: { phase: 'resetting', percent: 0 } });
      const r = await runExit({
        channel: controlSocket,
        release: doRelease,
        navigate,
        recording: rec,
        onStep: (step) => alive.current && setUi({ k: 'exiting', step }),
      });
      if (alive.current && !r.left) setUi({ k: 'failed', text: resetErrorText(r), exiting: true });
    },
    [controlSocket, doRelease, navigate],
  );

  const onExit = () => (recording ? setUi({ k: 'ask_recording' }) : void exit(null));
  const leaveAnyway = () => {
    setUi({ k: 'exiting', step: { phase: 'releasing' } });
    void leave({ release: doRelease, navigate, onStep: (step) => alive.current && setUi({ k: 'exiting', step }) });
  };

  const btn = `${compact ? 'h-11 rounded-sm px-3 text-[15px]' : 'min-h-12 rounded-lg px-4 py-3 text-base'} shrink-0 whitespace-nowrap font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-40`;
  const quiet = `${btn} bg-transparent text-zinc-700 ring-1 ring-inset ring-zinc-300 hover:bg-zinc-100 dark:text-zinc-200 dark:ring-zinc-700 dark:hover:bg-zinc-800`;
  const solid = `${btn} bg-zinc-900 text-white hover:bg-zinc-700 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300`;

  const resetting = prog.resetting || resetBusy;
  const resetText = prog.resetting ? resetLabel(prog) : resetBusy ? 'Resetting…' : 'Reset arm';
  const exitText =
    ui.k !== 'exiting'
      ? 'Exit'
      : ui.step.phase === 'recording'
        ? ui.step.action === 'discard' ? 'Discarding…' : 'Saving…'
        : ui.step.phase === 'resetting'
          ? `Resetting… ${Math.max(ui.step.percent, prog.percent ?? 0)}%`
          : 'Leaving…';

  return (
    <div className="relative flex shrink-0 items-stretch gap-1.5" data-testid="arm-session-buttons">
      <button
        type="button"
        className={quiet}
        data-testid="arm-reset"
        onClick={() => void onReset()}
        disabled={!open || stopped || recording || resetting || exiting}
        title={recording ? 'Finish or discard the recording first' : 'Move the arm slowly back to its rest pose'}
      >
        {exiting ? 'Reset arm' : resetText}
      </button>
      <button type="button" className={solid} data-testid="arm-exit" onClick={onExit} disabled={exiting || resetBusy}>
        {exitText}
      </button>

      {(ui.k === 'ask_recording' || ui.k === 'failed') && (
        <div
          role="dialog"
          className="absolute bottom-full right-0 z-30 mb-2 w-72 max-w-[calc(100vw-2rem)] rounded-lg border border-zinc-300 bg-white p-3 text-sm text-zinc-900 shadow-lg dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-100"
          data-testid={ui.k === 'ask_recording' ? 'arm-exit-recording' : 'arm-session-error'}
        >
          {ui.k === 'ask_recording' ? (
            <>
              <p className="font-semibold">Discard this recording?</p>
              <p className="mt-1 text-zinc-600 dark:text-zinc-400">Or finish it to keep it, then leave. The arm resets either way.</p>
              <div className="mt-3 flex flex-wrap gap-2">
                <button type="button" className={`${btn} bg-red-600 text-white hover:bg-red-500`} data-testid="arm-exit-discard" onClick={() => void exit('discard')}>
                  Discard and exit
                </button>
                <button type="button" className={`${btn} bg-emerald-600 text-white hover:bg-emerald-500`} data-testid="arm-exit-finish" onClick={() => void exit('finish')}>
                  Finish and exit
                </button>
                <button type="button" className={quiet} onClick={() => setUi({ k: 'idle' })}>
                  Stay
                </button>
              </div>
            </>
          ) : (
            <>
              <p className="font-semibold">{ui.exiting ? 'The arm did not reset' : 'Could not reset the arm'}</p>
              <p className="mt-1 text-zinc-600 dark:text-zinc-400">{ui.text}</p>
              <div className="mt-3 flex flex-wrap gap-2">
                {ui.exiting && (
                  <button type="button" className={solid} data-testid="arm-exit-anyway" onClick={leaveAnyway}>
                    Leave anyway
                  </button>
                )}
                <button type="button" className={quiet} onClick={() => setUi({ k: 'idle' })}>
                  {ui.exiting ? 'Stay' : 'OK'}
                </button>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
