'use client';

// TDM-59: claiming an arm-control task (clean_teleop / recovery_takeover) unlocks after the
// practice-mode readiness check. Reset tasks and operators who already passed see `children`
// (the normal claim button) unchanged.
//
//   <Tdm59ClaimGate taskType={t.type} taskId={t.id}>{claimButton}</Tdm59ClaimGate>

import Link from 'next/link';
import type { ReactNode } from 'react';
import { ARM_CONTROL_TYPES } from '@/lib/marketplace/tdm61';
import { useTdm59Readiness } from '@/lib/teleop/tdm59Readiness';

export function Tdm59ClaimGate({ taskType, taskId, children, primary }: { taskType: string; taskId: bigint | number | string; children: ReactNode; primary?: boolean }) {
  const { status } = useTdm59Readiness();
  if (!ARM_CONTROL_TYPES.includes(taskType) || status === 'ready') return <>{children}</>;
  if (status === 'unknown') {
    return <div className="h-[50px] w-full animate-pulse rounded-md bg-zinc-100 dark:bg-zinc-800" aria-hidden />;
  }
  const next = `/marketplace?task=${String(taskId)}`;
  return (
    <div data-testid="tdm59-claim-gate">
      <Link
        href={`/practice?next=${encodeURIComponent(next)}`}
        className={
          primary // TDM-86: the task a QR opened: practice is the next step, so it is the one primary button
            ? 'gx-btn gx-btn-primary gx-btn-block'
            : 'block w-full rounded-md border-2 border-dashed border-zinc-400 px-4 py-2.5 text-center text-base font-semibold text-zinc-800 outline-none hover:border-zinc-900 focus-visible:ring-2 focus-visible:ring-zinc-900 dark:border-zinc-600 dark:text-zinc-100 dark:hover:border-zinc-300'
        }
      >
        Practise to unlock
      </Link>
      <p className="mt-1.5 text-center text-xs text-zinc-500">
        About a minute on a simulated arm, once. Nothing is recorded and no one is waiting on you.
      </p>
    </div>
  );
}
