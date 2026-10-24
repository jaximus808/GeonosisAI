'use client';

import Link from 'next/link';
import { fmtCents } from '@/lib/format';
import { ButtonLink } from '@/components/brand/Button';
import { StatusTag, Tag } from '@/components/brand/StatusTag';
import { FakeBadge, SimBadge } from './Badges';
import { isFakeRobot, shortUri, type Robot, type Ticket } from './util';

type Dataset = { acceptedCount: number; totalCount: number } | undefined;

export function TicketHeader({ ticket, robot, dataset, mode, maxIterations }: {
  ticket: Ticket; robot?: Robot; dataset: Dataset;
  /** TDM-109: shown as "Iteration 2 of 3". */
  maxIterations?: number;
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
    <header className="mb-6">
      <div className="flex flex-wrap items-end justify-between gap-6">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="gx-id">TKT-{ticket.id.toString().padStart(4, '0')}</span>
            <StatusTag status={ticket.status} />
            {ticket.iteration > 0 && (
              <span className="gx-small">
                Iteration {ticket.iteration}
                {maxIterations ? ` of ${maxIterations}` : ''}
              </span>
            )}
            {fake && <FakeBadge title="This ticket runs on the fake-robot harness. Every trial, episode and number on this page is simulated." />}
            {mode === 'sim' && <SimBadge title="This ticket runs on the SIM robot (MuJoCo YAM in its 3D scene). Switch it to the real robot in Run settings." />}
            {mode === 'real' && <Tag tone="cancelled">Real robot</Tag>}
          </div>
          <h1 className="gx-display-l mt-1 mb-2 max-w-4xl">{ticket.taskDescription}</h1>
          <div className="gx-small flex flex-wrap gap-x-4 gap-y-1">
            {robot ? (
              <span>
                Robot{' '}
                <Link href={`/robots/${robot.id}`} className="gx-data text-ink hover:underline">
                  {robot.name}
                </Link>{' '}
                ({robot.embodiment})
              </span>
            ) : (
              <span>
                Robot <span className="gx-data text-ink">#{ticket.robotId.toString()}</span>
              </span>
            )}
            <span>Target {Math.round(ticket.targetSuccess * 100)}%</span>
            <span>Training set {dataset ? `${dataset.acceptedCount} accepted of ${dataset.totalCount}` : '—'}</span>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <ButtonLink href={`/tickets/${ticket.id}/report`}>View report</ButtonLink>
        </div>
      </div>
      {mode === 'sim' && (
        <p className="gx-frame-sim mt-4 mb-0 max-w-3xl rounded-sm bg-steel-soft px-3 py-2 text-sm text-ink">
          <span className="gx-station-label mr-2 font-stencil text-steel">Simulated</span>
          This ticket runs on the MuJoCo sim robot: its baseline, takeovers and evals happen in the 3D scene, and its numbers are sim
          results, never real-arm success rates.
        </p>
      )}
      {fake && (
        <p className="mt-3 mb-0 max-w-3xl rounded-sm border-l-4 border-slag bg-slag-soft px-3 py-2 text-sm text-ink">
          Fake robot. This ticket was driven by the simulator harness, not the YAM arm, so its success rates are not results.
        </p>
      )}

      <div className="mt-5 max-w-xl">
        <div className="gx-small flex justify-between">
          <span className="gx-label">Spend</span>
          <span className="gx-data text-ochre">
            {fmtCents(spent)} <span className="text-ink-muted">of {fmtCents(budget)}</span>
          </span>
        </div>
        <div
          className="gx-meter mt-2"
          role="img"
          aria-label={`Spent ${fmtCents(spent)}, approved ${fmtCents(approved)}, budget ${fmtCents(budget)}`}
        >
          <span style={{ width: w(approved), opacity: 0.35 }} />
          <span style={{ width: w(spent) }} />
        </div>
        <p className="gx-small mt-1.5 mb-0">
          <span className="gx-data">{fmtCents(approved)}</span> approved for data plans; spent counts reserved and paid payouts. Test mode.
        </p>
      </div>
      {ticket.deployedCheckpointUri && (
        <p className="mt-4 mb-0 text-sm">
          <span className="text-ink-muted">Deployed checkpoint </span>
          <code className="gx-data break-all" title={ticket.deployedCheckpointUri}>{shortUri(ticket.deployedCheckpointUri, 4)}</code>
        </p>
      )}
      {terminal && ticket.status !== 'shipped' && (
        <p className="mt-3 mb-0 text-sm text-slag">This ticket is {ticket.status}.</p>
      )}
    </header>
  );
}
