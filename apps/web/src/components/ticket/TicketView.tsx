'use client';

import { useMemo } from 'react';
import { useRows, useTicket } from '@/lib/spacetime/hooks';
import { Block, TestModeBadge } from './Badges';
import { DecisionCard } from './DecisionCard';
import { EpisodesGrid } from './EpisodesGrid';
import { EvalCards } from './EvalCards';
import { OfferCard } from './OfferCard';
import { PaymentsTable, TasksTable } from './TasksTable';
import { TicketHeader } from './TicketHeader';
import { TimelineStrip } from './TimelineStrip';
import { TrainingRuns } from './TrainingRuns';
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
  const [clusters] = useRows('failureCluster', (r) => r.ticketId.eq(tid));
  const [plans] = useRows('dataPlan', (r) => r.ticketId.eq(tid));
  const [tasks] = useRows('task', (r) => r.ticketId.eq(tid));
  const [episodes] = useRows('episode', (r) => r.ticketId.eq(tid));
  const [reviews] = useRows('review', (r) => r.ticketId.eq(tid));
  const [payments] = useRows('payment', (r) => r.ticketId.eq(tid));
  const [trainingRuns] = useRows('trainingRun', (r) => r.ticketId.eq(tid));
  const [offers] = useRows('datasetOffer', (r) => r.ticketId.eq(tid));
  const [contributors] = useRows('contributor');
  const [eventRows] = useRows('eventLog');

  const events = useMemo(() => ticketEvents(eventRows, tid), [eventRows, tid]);
  const people = useMemo(() => new Map(contributors.map((c) => [c.id, c])), [contributors]);
  const episodeMap = useMemo(() => new Map(episodes.map((e) => [e.id, e])), [episodes]);
  const iterationOf = useMemo(() => new Map(trainingRuns.map((r) => [r.id, r.iteration])), [trainingRuns]);

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

      <Block title="Before and after" aside={<span className="text-sm text-zinc-500">task success per eval run, live as trials finish</span>}>
        <EvalCards runs={evalRuns} events={events} settings={settings} robotFake={robotFake} iterationOf={iterationOf} />
      </Block>

      <Block title="Data plan">
        <DecisionCard plans={plans} clusters={clusters} episodes={episodeMap} events={events} />
      </Block>

      <Block title="Paid tasks">
        <TasksTable tasks={tasks} people={people} />
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
