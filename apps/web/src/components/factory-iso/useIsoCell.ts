'use client';

// TDM-115: live data for one robot cell. The TDM-112 floor model (useTdm112Floor) supplies the stations
// and droids, so whatever TDM-114 adds to it shows up here too; the machines read a few more rows
// directly. Every input is a SpacetimeDB subscription. The model is pure (lib/tdm115/iso.ts).

import { useMemo } from 'react';
import { useRows } from '@/lib/spacetime/hooks';
import { tdm112SplitEvents, useTdm112Floor, useTdm112Now } from '@/lib/tdm112/useFloor';
import { tdm115Cell, tdm115Holder, tdm115RobotQueue, type Tdm115Cell, type Tdm115QueueItem } from '@/lib/tdm115/iso';
import type { Tdm112Event, Tdm112Floor } from '@/lib/tdm112/floor';

export interface IsoCellData {
  cell: Tdm115Cell | null;
  floor: Tdm112Floor | null;
  events: Tdm112Event[];
  queue: Tdm115QueueItem[];
  ready: boolean;
  taskDescription?: string;
}

export function useIsoCell(ticketId: bigint): IsoCellData {
  const tid = ticketId;
  const { floor, ready, events, taskDescription } = useTdm112Floor(tid);
  const [tickets] = useRows('ticket', (r) => r.id.eq(tid));
  const ticket = tickets[0];
  const rid = ticket?.robotId ?? 0n;
  const [robots] = useRows('robot', (r) => r.id.eq(rid));
  const [commands] = useRows('robotCommand', (r) => r.robotId.eq(rid));
  const [robotTickets] = useRows('ticket', (r) => r.robotId.eq(rid));
  const [evalRuns] = useRows('evalRun', (r) => r.ticketId.eq(tid));
  const [evalTrials] = useRows('evalTrial');
  const [clusters] = useRows('failureCluster', (r) => r.ticketId.eq(tid));
  const [plans] = useRows('dataPlan', (r) => r.ticketId.eq(tid));
  const [tasks] = useRows('task', (r) => r.ticketId.eq(tid));
  const [episodes] = useRows('episode', (r) => r.ticketId.eq(tid));
  const [reviews] = useRows('review', (r) => r.ticketId.eq(tid));
  const [trainingRuns] = useRows('trainingRun', (r) => r.ticketId.eq(tid));
  const [eventRows] = useRows('eventLog');
  const llmCalls = useMemo(() => tdm112SplitEvents(eventRows, Number(tid)).llmCalls, [eventRows, tid]);
  const runIds = useMemo(() => new Set(evalRuns.map((r) => r.id)), [evalRuns]);
  const myTrials = useMemo(() => evalTrials.filter((t) => runIds.has(t.evalRunId)), [evalTrials, runIds]);
  const now = useTdm112Now(2000);
  const robot = robots[0] ?? null;

  const cell = useMemo(() => {
    if (!floor || !ticket) return null;
    return tdm115Cell({
      floor,
      ticket,
      robot,
      commands,
      robotTickets,
      evalRuns,
      evalTrials: myTrials,
      clusters,
      plans,
      tasks,
      episodes,
      reviews,
      trainingRuns,
      events,
      llmCalls,
      nowUs: now * 1000,
    });
  }, [floor, ticket, robot, commands, robotTickets, evalRuns, myTrials, clusters, plans, tasks, episodes, reviews, trainingRuns, events, llmCalls, now]);

  const queue = useMemo(() => tdm115RobotQueue(robotTickets, tdm115Holder(robot, commands)), [robotTickets, robot, commands]);

  return { cell, floor, events, queue, ready, taskDescription };
}
