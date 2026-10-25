'use client';

// TDM-116: every live row the ticket cockpit shows, subscribed once at the shell and passed down, plus the
// TDM-112/114 floor model (stations, who holds the robot, eval phase). Nothing here is mocked.

import { useMemo } from 'react';
import { useRows, useTicket } from '@/lib/spacetime/hooks';
import { useTdm112Floor } from '@/lib/tdm112/useFloor';
import type { Tdm114LivePolicy } from '@/lib/tdm114/arbitration';
import { tdm107ModeOf, tdm107SimIds } from '@/lib/tdm107/robotMode';
import { isFakeRobot, parseObj, ticketEvents, type EvalRun } from '@/components/ticket/util';
import type { Tdm116EventLike } from '@/lib/tdm116/cockpit';

export function useTdm116Cockpit(tid: bigint, opts: { livePolicy?: Tdm114LivePolicy | null } = {}) {
  const [ticket, ready] = useTicket(tid < 0n ? 0n : tid);
  const robotId = ticket?.robotId ?? 0n;
  const [allRobots] = useRows('robot');
  const [settingsRows] = useRows('runSettings', (r) => r.ticketId.eq(tid));
  const [datasets] = useRows('dataset', (r) => r.ticketId.eq(tid));
  const [evalRuns] = useRows('evalRun', (r) => r.ticketId.eq(tid));
  const [allTrials] = useRows('evalTrial');
  const [tasks] = useRows('task', (r) => r.ticketId.eq(tid));
  const [episodes] = useRows('episode', (r) => r.ticketId.eq(tid));
  const [reviews] = useRows('review', (r) => r.ticketId.eq(tid));
  const [payments] = useRows('payment', (r) => r.ticketId.eq(tid));
  const [trainingRuns] = useRows('trainingRun', (r) => r.ticketId.eq(tid));
  const [plans] = useRows('dataPlan', (r) => r.ticketId.eq(tid));
  const [clusters] = useRows('failureCluster', (r) => r.ticketId.eq(tid));
  const [offers] = useRows('datasetOffer', (r) => r.ticketId.eq(tid));
  const [contributors] = useRows('contributor');
  const [eventRows] = useRows('eventLog');
  const { floor, events: floorEvents } = useTdm112Floor(tid < 0n ? 0n : tid, { livePolicy: opts.livePolicy });

  const robot = useMemo(() => allRobots.find((r) => r.id === robotId), [allRobots, robotId]);
  const simRobotIds = useMemo(() => tdm107SimIds(allRobots), [allRobots]);
  const events = useMemo(() => ticketEvents(eventRows, tid), [eventRows, tid]);
  const people = useMemo(() => new Map(contributors.map((c) => [c.id, c])), [contributors]);
  const iterationOf = useMemo(() => new Map(trainingRuns.map((r) => [r.id, r.iteration])), [trainingRuns]);
  const runIds = useMemo(() => new Set(evalRuns.map((r) => r.id)), [evalRuns]);
  const trials = useMemo(() => allTrials.filter((t) => runIds.has(t.evalRunId)), [allTrials, runIds]);
  const settings = settingsRows[0];
  const mode = tdm107ModeOf(robot);

  // Job drops and claims: job_drop.claimed carries no ticket id, so these are read from every row and
  // matched by task id in tdm116Drops.
  const dropEvents = useMemo(() => {
    const out: Tdm116EventLike[] = [];
    for (const r of eventRows) {
      if (!(r.type.startsWith('job_drop.') || r.type === 'text.offer_sent')) continue;
      const p = parseObj(r.payloadJson);
      if (p) out.push({ type: r.type, payload: p });
    }
    return out;
  }, [eventRows]);

  // TDM-107: the before/after headline uses only runs on the ticket's current mode (never pools sim and real).
  const { baseline, candidate } = useMemo(() => {
    const sameMode = (r: EvalRun) => !mode || simRobotIds.has(r.robotId) === (mode === 'sim');
    const byNew = [...evalRuns].sort((a, b) => Number(b.createdAt.microsSinceUnixEpoch - a.createdAt.microsSinceUnixEpoch));
    return {
      baseline: byNew.find((r) => r.kind === 'baseline' && sameMode(r)),
      candidate: byNew.find((r) => r.kind === 'candidate' && sameMode(r)),
    };
  }, [evalRuns, mode, simRobotIds]);

  // TDM-79: what the run-settings drawer needs to say which iteration picks a change up.
  const loop = useMemo(
    () => ({
      status: ticket?.status ?? 'created',
      iteration: ticket?.iteration ?? 0,
      maxIterations: settings?.maxIterations ?? 3,
      trainedIterations: new Set(trainingRuns.map((r) => r.iteration)),
      hasBaselineEval: evalRuns.some((r) => r.kind === 'baseline'),
      plannedIterations: new Set(plans.map((p) => p.iteration)),
      evaluatedIterations: new Set(
        evalRuns.filter((r) => r.kind !== 'baseline' && iterationOf.has(r.trainingRunId)).map((r) => iterationOf.get(r.trainingRunId)!),
      ),
    }),
    [ticket?.status, ticket?.iteration, settings, trainingRuns, evalRuns, iterationOf, plans],
  );

  return {
    tid,
    ready,
    ticket,
    robot,
    robotId,
    robotFake: isFakeRobot(robot),
    mode,
    allRobots,
    simRobotIds,
    settings,
    dataset: datasets[0],
    evalRuns,
    trials,
    baseline,
    candidate,
    tasks,
    episodes,
    reviews,
    payments,
    trainingRuns,
    plans,
    clusters,
    offers,
    contributors,
    people,
    iterationOf,
    events,
    floorEvents,
    dropEvents,
    floor,
    loop,
  };
}

export type Tdm116Data = ReturnType<typeof useTdm116Cockpit>;
