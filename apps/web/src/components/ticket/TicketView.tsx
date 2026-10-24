'use client';

import { useMemo } from 'react';
import { useRows, useTicket } from '@/lib/spacetime/hooks';
import { JoinQr } from '@/components/JoinQr';
import { TicketSpendPanel } from '@/components/payments/TicketSpendPanel';
import { Block, TestModeBadge } from './Badges';
import { DecisionTrail } from './DecisionTrail';
import { EpisodesGrid } from './EpisodesGrid';
import { EvalCards } from './EvalCards';
import { OfferCard } from './OfferCard';
import { PaymentsTable, TasksTable } from './TasksTable';
import { TicketHeader } from './TicketHeader';
import { TimelineStrip } from './TimelineStrip';
import { TrainingRuns } from './TrainingRuns';
import { RunSettingsButton } from './RunSettingsDrawer';
import { isFakeRobot, ticketEvents } from './util';

/** TDM-75: the live ticket page. Every section is a SpacetimeDB subscription; nothing here is mocked. */
export function TicketView({ id }: { id: string }) {
  const tid = useMemo(() => {
    try {
      return BigInt(id);
    } catch {
      return -1n;
    }
  }, [id]);
  const [ticket, ready] = useTicket(tid < 0n ? 0n : tid);
  const robotId = ticket?.robotId ?? 0n;
  const [robots] = useRows('robot', (r) => r.id.eq(robotId));
  const [settingsRows] = useRows('runSettings', (r) => r.ticketId.eq(tid));
  const [datasets] = useRows('dataset', (r) => r.ticketId.eq(tid));
  const [evalRuns] = useRows('evalRun', (r) => r.ticketId.eq(tid));
  const [tasks] = useRows('task', (r) => r.ticketId.eq(tid));
  const [episodes] = useRows('episode', (r) => r.ticketId.eq(tid));
  const [reviews] = useRows('review', (r) => r.ticketId.eq(tid));
  const [payments] = useRows('payment', (r) => r.ticketId.eq(tid));
  const [trainingRuns] = useRows('trainingRun', (r) => r.ticketId.eq(tid));
  const [plans] = useRows('dataPlan', (r) => r.ticketId.eq(tid));
  const [offers] = useRows('datasetOffer', (r) => r.ticketId.eq(tid));
  const [contributors] = useRows('contributor');
  const [eventRows] = useRows('eventLog');

  const events = useMemo(() => ticketEvents(eventRows, tid), [eventRows, tid]);
  const people = useMemo(() => new Map(contributors.map((c) => [c.id, c])), [contributors]);
  const iterationOf = useMemo(() => new Map(trainingRuns.map((r) => [r.id, r.iteration])), [trainingRuns]);
  // TDM-79: what the settings drawer needs to say which iteration picks a change up.
  const loop = useMemo(
    () => ({
      status: ticket?.status ?? 'created',
      iteration: ticket?.iteration ?? 0,
      maxIterations: settingsRows[0]?.maxIterations ?? 3,
      trainedIterations: new Set(trainingRuns.map((r) => r.iteration)),
      hasBaselineEval: evalRuns.some((r) => r.kind === 'baseline'),
      plannedIterations: new Set(plans.map((p) => p.iteration)),
      evaluatedIterations: new Set(
        evalRuns.filter((r) => r.kind !== 'baseline' && iterationOf.has(r.trainingRunId)).map((r) => iterationOf.get(r.trainingRunId)!),
      ),
    }),
    [ticket?.status, ticket?.iteration, settingsRows, trainingRuns, evalRuns, iterationOf, plans],
  );

  if (tid < 0n) return <p className="text-sm text-zinc-500">“{id}” is not a ticket number.</p>;
  if (!ready) return <p className="text-sm text-zinc-500">Connecting to the ticket…</p>;
  if (!ticket) {
    return (
      <p className="rounded-md border border-dashed border-zinc-300 px-4 py-3 text-sm text-zinc-500 dark:border-zinc-700">
        There is no ticket #{id} in this database. It appears here as soon as it is created.
      </p>
    );
  }

  const robot = robots[0];
  const settings = settingsRows[0];
  const robotFake = isFakeRobot(robot);

  return (
    <>
      <TicketHeader ticket={ticket} robot={robot} dataset={datasets[0]} />
      <TimelineStrip ticket={ticket} events={events} />

      {/* TDM-79: run-settings editor (drawer). The report is linked from the header. */}
      <div className="-mt-4 mb-8">
        <RunSettingsButton ticketId={id} loop={loop} liveUpdatedAtMicros={settings?.updatedAt.microsSinceUnixEpoch} />
      </div>

      <Block title="Before and after" aside={<span className="text-sm text-zinc-500">task success per eval run, live as trials finish</span>}>
        <EvalCards runs={evalRuns} events={events} settings={settings} robotFake={robotFake} iterationOf={iterationOf} />
      </Block>

      {/* TDM-68 evidence trail, preferred over DecisionCard (whose rejected-alternative line reads a
          field the planner never writes). Re-fetches whenever a ticket event lands. */}
      <Block title="Data plan" aside={<span className="text-sm text-zinc-500">failed trials, clusters, the planner&apos;s reasoning, what was bought</span>}>
        <DecisionTrail ticketId={id} refreshKey={events.length} />
      </Block>

      {/* TDM-64 spend and budget, live. */}
      <div className="mb-10">
        <TicketSpendPanel ticketId={tid} />
      </div>

      <Block title="Paid tasks">
        <div className="flex flex-col gap-6 lg:flex-row lg:items-start">
          <div className="min-w-0 flex-1">
            <TasksTable tasks={tasks} people={people} />
          </div>
          {/* TDM-60: phones scan this to join and pick up this ticket's paid tasks. */}
          <div className="shrink-0">
            <JoinQr ticketId={id} />
          </div>
        </div>
      </Block>

      <Block title="Payouts" aside={<TestModeBadge />}>
        <PaymentsTable payments={payments} people={people} />
      </Block>

      {offers.length > 0 && (
        <Block title="Dataset offers">
          <OfferCard offers={offers} people={people} />
        </Block>
      )}

      <Block title="Episodes and reviews" id="episodes">
        <EpisodesGrid episodes={episodes} reviews={reviews} payments={payments} people={people} />
      </Block>

      <Block title="Training runs">
        <TrainingRuns runs={trainingRuns} />
      </Block>
    </>
  );
}
