'use client';

import Link from 'next/link';
import { fmtCents } from '@/lib/format';
import { FakeBadge, SimBadge, StatusPill } from './Badges';
import { isFakeRobot, shortUri, stageLabel, type Robot, type Ticket } from './util';

type Dataset = { acceptedCount: number; totalCount: number } | undefined;

export function TicketHeader({ ticket, robot, dataset, mode }: {
  ticket: Ticket; robot?: Robot; dataset: Dataset;
  /** TDM-107: the ticket's robot mode (from its robot). */
  mode?: 'sim' | 'real' | null;
}) {
  const fake = isFakeRobot(robot);
  const budget = Number(ticket.budgetCents);
  const spent = Number(ticket.spentCents);
  const approved = Number(ticket.approvedCents);
  const w = (c: number) => (budget > 0 ? `${Math.min(100, (c / budget) * 100)}%` : '0%');
  const terminal = ['shipped', 'failed', 'cancelled'].includes(ticket.status);

  return (
    <header className="mb-8">
      <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-zinc-500">
        <span className="tabular-nums">Ticket #{ticket.id.toString()}</span>
        <span aria-hidden>/</span>
        {robot ? (
          <Link href={`/robots/${robot.id}`} className="hover:text-zinc-900 hover:underline dark:hover:text-zinc-100">
            {robot.name} <span className="text-zinc-400">({robot.embodiment})</span>
          </Link>
        ) : (
          <span>robot #{ticket.robotId.toString()}</span>
        )}
        {fake && <FakeBadge title="This ticket runs on the fake-robot harness. Every trial, episode and number on this page is simulated." />}
        {mode === 'sim' && <SimBadge title="This ticket runs on the SIM robot (MuJoCo YAM in its 3D scene). Switch it to the real robot in Run settings." />}
        {mode === 'real' && <span className="rounded-sm border border-zinc-300 px-1.5 py-px text-[11px] font-semibold text-zinc-600 dark:border-zinc-700 dark:text-zinc-300">REAL ROBOT</span>}
        <Link href={`/tickets/${ticket.id}/report`} className="ml-auto text-blue-600 hover:underline">
          Report
        </Link>
      </div>
      <h1 className="max-w-4xl text-3xl font-semibold leading-tight tracking-tight sm:text-4xl">{ticket.taskDescription}</h1>
      {mode === 'sim' && (
        <p className="mt-3 max-w-3xl rounded border-l-4 border-violet-600 bg-violet-50 px-3 py-2 text-sm text-violet-900 dark:bg-violet-950/40 dark:text-violet-200">
          SIMULATED. This ticket runs on the MuJoCo sim robot: its baseline, takeovers and evals happen in the 3D scene, and
          its numbers are sim results, never real-arm success rates.
        </p>
      )}
      {fake && (
        <p className="mt-3 max-w-3xl rounded border-l-4 border-fuchsia-600 bg-fuchsia-50 px-3 py-2 text-sm text-fuchsia-900 dark:bg-fuchsia-950/40 dark:text-fuchsia-200">
          Fake robot. This ticket was driven by the simulator harness, not the YAM arm, so its success rates are not results.
        </p>
      )}

      <dl className="mt-5 grid grid-cols-2 gap-x-8 gap-y-4 text-sm sm:grid-cols-4 lg:grid-cols-[auto_auto_auto_auto_minmax(16rem,1fr)]">
        <div>
          <dt className="text-zinc-500">Stage</dt>
          <dd className="mt-1">
            <StatusPill status={stageLabel(ticket.status)} tone={ticket.status === 'shipped' ? 'good' : ticket.status === 'failed' || ticket.status === 'cancelled' ? 'bad' : 'busy'} />
          </dd>
        </div>
        <div>
          <dt className="text-zinc-500">Iteration</dt>
          <dd className="mt-1 tabular-nums">{ticket.iteration}</dd>
        </div>
        <div>
          <dt className="text-zinc-500">Target</dt>
          <dd className="mt-1 tabular-nums">{Math.round(ticket.targetSuccess * 100)}% success</dd>
        </div>
        <div>
          <dt className="text-zinc-500">Training set</dt>
          <dd className="mt-1 tabular-nums">
            {dataset ? `${dataset.acceptedCount} accepted of ${dataset.totalCount}` : '—'}
          </dd>
        </div>
        <div className="col-span-2 sm:col-span-4 lg:col-span-1">
          <dt className="flex justify-between text-zinc-500">
            <span>Spend</span>
            <span className="tabular-nums text-zinc-900 dark:text-zinc-100">
              {fmtCents(spent)} <span className="text-zinc-500">of {fmtCents(budget)}</span>
            </span>
          </dt>
          <dd className="mt-2">
            <div
              className="relative h-2.5 overflow-hidden rounded-full bg-zinc-200 dark:bg-zinc-800"
              role="img"
              aria-label={`Spent ${fmtCents(spent)}, approved ${fmtCents(approved)}, budget ${fmtCents(budget)}`}
            >
              <div className="absolute inset-y-0 left-0 bg-zinc-400/60 dark:bg-zinc-600" style={{ width: w(approved) }} />
              <div className="absolute inset-y-0 left-0 bg-zinc-900 dark:bg-zinc-100" style={{ width: w(spent) }} />
            </div>
            <p className="mt-1.5 text-xs text-zinc-500">
              <span className="tabular-nums">{fmtCents(approved)}</span> approved for data plans; spent counts reserved and paid payouts.
            </p>
          </dd>
        </div>
      </dl>
      {ticket.deployedCheckpointUri && (
        <p className="mt-4 text-sm">
          <span className="text-zinc-500">Deployed checkpoint </span>
          <code className="break-all text-xs" title={ticket.deployedCheckpointUri}>{shortUri(ticket.deployedCheckpointUri, 4)}</code>
        </p>
      )}
      {terminal && ticket.status !== 'shipped' && (
        <p className="mt-3 text-sm text-red-700 dark:text-red-400">This ticket is {ticket.status}.</p>
      )}
    </header>
  );
}
