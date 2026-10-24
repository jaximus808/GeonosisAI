'use client';

// Teleop integration (TDM-56 page + TDM-57 lease + TDM-58 recording + TDM-59 onboarding).
//
// /teleop/[taskId] mounts this. It wires the parallel pieces into the TDM-56 console's slots:
//   - TDM-57 useTeleopLease(taskId): canDrive / canDriveReason, every outgoing cmd and recording op
//     goes through lease.stamp(...), <ControlLeaseBanner/> in controlSlot
//   - TDM-58 <RecordingControls taskId kind controlSocket/> in recordingSlot (controlSocket is the
//     console's own /control socket: send + subscribe)
//   - TDM-59 <Tdm59TeleopOnboarding/> stays BEFORE the console (its key shield must register first).
//     While its walkthrough is open the console may not drive (gamepad RT is polled, not shielded).
//
// Dev links (?dev_url=...) bypass the lease: no claimed task, the console says so.

import { useEffect, useState } from 'react';
import { ControlLeaseBanner } from '@/components/teleop/ControlLease';
import { RecordingControls } from '@/components/teleop/RecordingControls';
import { Tdm56TeleopConsole, useTdm56Teleop } from '@/components/teleop/Tdm56TeleopConsole';
import { Tdm59TeleopOnboarding } from '@/components/teleop/Tdm59TeleopOnboarding';
import { LOST_TEXT, useTeleopLease, type TeleopLease } from '@/lib/teleop/tdm57Lease';

/** True while TDM-59's walkthrough dialog (data-testid="tdm59-intro") is on the page. */
function useWalkthroughOpen(): boolean {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    const check = () => setOpen(document.querySelector('[data-testid="tdm59-intro"]') !== null);
    check();
    const mo = new MutationObserver(check);
    mo.observe(document.body, { childList: true, subtree: true });
    return () => mo.disconnect();
  }, []);
  return open;
}

function leaseReason(l: TeleopLease): string {
  switch (l.state) {
    case 'held':
      return l.blockedBy === 'hidden'
        ? 'Tab is in the background.'
        : l.blockedBy === 'offline'
          ? 'Reconnecting to the server.'
          : 'Renewing your control lease.';
    case 'view_only':
      return `Controlled by ${l.holderName ?? 'someone else'}.`;
    case 'lost':
      return l.lostReason ? LOST_TEXT[l.lostReason] : 'You lost control.';
    case 'not_joined':
      return 'Join as a contributor to drive the arm.';
    case 'available':
      return 'Nobody holds control. Take control first.';
    case 'claiming':
      return 'Taking control…';
    case 'closed':
      return `This task is ${l.task?.status ?? 'closed'}.`;
    case 'no_task':
      return 'This task does not exist.';
    default:
      return 'Checking your control lease…';
  }
}

/** Inside the console's provider: hands TDM-58 the console's /control socket. */
function RecordingSlot({ taskId, kind }: { taskId: string; kind: string }) {
  const { controlSocket } = useTdm56Teleop();
  return <RecordingControls taskId={taskId} kind={kind} controlSocket={controlSocket} />;
}

export function TeleopSession({ taskId, dev }: { taskId: string; dev?: { streamUrl: string; token: string; controlUrl?: string } }) {
  const lease = useTeleopLease(taskId);
  const walkthrough = useWalkthroughOpen();
  const kind = lease.task?.type ?? 'clean_teleop';
  const isTaskId = /^\d+$/.test(taskId);

  const canDrive = (dev ? true : lease.canControl) && !walkthrough;
  const canDriveReason = walkthrough ? 'Close the "How to drive" walkthrough to drive.' : dev ? undefined : leaseReason(lease);

  return (
    <>
      {/* TDM-59: first-time operator help, connection indicator, session state. Keep it before the console. */}
      <Tdm59TeleopOnboarding taskId={taskId} />
      <Tdm56TeleopConsole
        taskId={taskId}
        dev={dev}
        canDrive={canDrive}
        canDriveReason={canDriveReason}
        stamp={dev ? undefined : lease.stamp}
        controlSlot={dev ? undefined : <ControlLeaseBanner lease={lease} className="py-2" />}
        recordingSlot={isTaskId && (dev || lease.task) ? <RecordingSlot taskId={taskId} kind={kind} /> : undefined}
      />
    </>
  );
}
