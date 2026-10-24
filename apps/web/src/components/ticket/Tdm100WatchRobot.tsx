'use client';

// TDM-100: the ticket page's "Watch the robot" section. Open while the robot is doing this ticket's
// work (baseline eval, data collection, eval), collapsed otherwise so the page isn't holding a
// state socket and a 3D canvas for nothing. A viewer's own open/close wins until the stage changes.

import { useEffect, useState } from 'react';
import { Tdm100RobotPanel, type Tdm100RobotPanelProps } from '@/components/robotview/Tdm100RobotPanel';
import { Block } from './Badges';

/** Ticket stages where the arm is moving for this ticket. */
export const TDM100_WATCH_STAGES: Record<string, string> = {
  baseline: 'Baseline eval running',
  dispatch: 'Collecting data',
  eval: 'Eval running',
};

export function Tdm100WatchRobot({
  status,
  robotId,
  robot,
  ticketId,
}: {
  status: string;
  robotId: bigint;
  robot?: Tdm100RobotPanelProps['robot'];
  ticketId: bigint;
}) {
  const stage = TDM100_WATCH_STAGES[status];
  const [userOpen, setUserOpen] = useState<boolean | null>(null);
  useEffect(() => setUserOpen(null), [status]);
  const open = userOpen ?? !!stage;
  const hasRobot = robotId > 0n;

  const toggle = (
    <button
      type="button"
      aria-expanded={open}
      aria-controls="tdm100-watch-robot"
      onClick={() => setUserOpen(!open)}
      className="rounded-md px-2 py-0.5 text-sm font-medium text-sky-700 hover:bg-sky-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-sky-500 dark:text-sky-400 dark:hover:bg-sky-950/40"
    >
      {open ? 'Hide' : 'Show'}
    </button>
  );

  return (
    <Block
      title="Watch the robot"
      id="watch-robot"
      aside={
        <span className="flex items-baseline gap-2 text-sm text-zinc-500">
          {stage ? (
            <span className="font-medium text-sky-700 dark:text-sky-400">{stage}</span>
          ) : (
            <span>the arm isn&apos;t working on this ticket right now</span>
          )}
          {hasRobot && toggle}
        </span>
      }
    >
      <div id="tdm100-watch-robot">
        {!hasRobot ? (
          <p className="text-sm text-zinc-500">No robot is assigned to this ticket yet.</p>
        ) : open ? (
          <Tdm100RobotPanel robotId={robotId} robot={robot} ticketId={ticketId} />
        ) : null}
      </div>
    </Block>
  );
}
