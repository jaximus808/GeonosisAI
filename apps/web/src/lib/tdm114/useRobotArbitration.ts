'use client';

// TDM-114: live holder + queue for one robot (RobotView HUD, robot page). Same pure model as the floor.

import { useMemo } from 'react';
import { useRows } from '@/lib/spacetime/hooks';
import { tdm114RobotEvents } from '@/lib/tdm112/useFloor';
import { tdm114Holder, tdm114Queue, type Tdm114Holder, type Tdm114QueueEntry } from '@/lib/tdm112/floor';

export function useTdm114Robot(robotId: number | bigint | null | undefined, thisTicketId?: number | bigint | null): {
  holder: Tdm114Holder | null;
  queue: Tdm114QueueEntry[];
  robotStatus?: string;
} {
  const rid = robotId == null ? 0n : BigInt(robotId);
  const [robots] = useRows('robot', (r) => r.id.eq(rid));
  const [cmds] = useRows('robotCommand', (r) => r.robotId.eq(rid));
  const [tickets] = useRows('ticket', (r) => r.robotId.eq(rid));
  const [runs] = useRows('evalRun', (r) => r.robotId.eq(rid));
  const [events] = useRows('eventLog');
  const robotEvents = useMemo(() => tdm114RobotEvents(events, Number(rid)), [events, rid]);
  const robot = robots[0];
  return useMemo(() => {
    const open = cmds.filter((c) => c.status === 'issued' || c.status === 'acked');
    const holder = tdm114Holder({ robot, commands: open, robotEvents });
    const queue = robot ? tdm114Queue({ holder, tickets, runs, robotEvents, thisTicketId: Number(thisTicketId ?? 0) }) : [];
    return { holder, queue, robotStatus: robot?.status };
  }, [robot, cmds, tickets, runs, robotEvents, thisTicketId]);
}
