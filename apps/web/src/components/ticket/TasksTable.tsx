'use client';

import { fmtCents, fmtTs } from '@/lib/format';
import { Empty, StatusPill } from './Badges';
import { TASK_TYPE_LABEL, byNewest, fmtTime, type Contributor, type Payment, type Task } from './util';

const th = 'px-3 py-2 font-medium';
const td = 'px-3 py-2';

/** Paid marketplace tasks for this ticket, newest first. */
export function TasksTable({ tasks, people }: { tasks: readonly Task[]; people: Map<bigint, Contributor> }) {
  if (tasks.length === 0) return <Empty>No paid tasks yet. They go out to contributors once a data plan is approved.</Empty>;
  const open = tasks.filter((t) => t.status === 'open').length;
  const claimed = tasks.filter((t) => t.status === 'claimed').length;
  const done = tasks.filter((t) => t.status === 'completed' || t.status === 'submitted').length;
  return (
    <div>
      <p className="mb-2 text-sm tabular-nums text-zinc-500">
        {open} open, {claimed} in progress, {done} done
      </p>
      <div className="overflow-x-auto rounded-md border border-zinc-200 dark:border-zinc-800">
        <table className="w-full text-left text-sm">
          <thead className="bg-zinc-50 text-xs text-zinc-500 dark:bg-zinc-900">
            <tr>
              <th className={th}>#</th>
              <th className={th}>Type</th>
              <th className={`${th} text-right`}>Price</th>
              <th className={th}>Assignee</th>
              <th className={th}>Status</th>
              <th className={th}>Claimed</th>
              <th className={th}>Done</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-100 dark:divide-zinc-800">
            {byNewest(tasks).map((t) => {
              const who = t.assigneeId ? people.get(t.assigneeId) : undefined;
              const leaseLeft = t.status === 'claimed' && t.leaseExpiresAt ? t.leaseExpiresAt.toDate().getTime() - Date.now() : undefined;
              return (
                <tr key={t.id.toString()} className="align-top">
                  <td className={`${td} tabular-nums text-zinc-400`}>{t.id.toString()}</td>
                  <td className={td} title={t.instructions || undefined}>{TASK_TYPE_LABEL[t.type] ?? t.type}</td>
                  <td className={`${td} text-right tabular-nums`}>{fmtCents(t.priceCents)}</td>
                  <td className={td}>{who ? who.name : t.assigneeId ? `#${t.assigneeId}` : <span className="text-zinc-400">unassigned</span>}</td>
                  <td className={td}>
                    <StatusPill status={t.status} />
                    {leaseLeft !== undefined && leaseLeft < 0 && <span className="ml-1 text-xs text-amber-700">lease lapsed</span>}
                  </td>
                  <td className={`${td} whitespace-nowrap text-xs text-zinc-500`} title={t.claimedAt ? fmtTs(t.claimedAt) : undefined}>{fmtTime(t.claimedAt)}</td>
                  <td className={`${td} whitespace-nowrap text-xs text-zinc-500`} title={t.completedAt ? fmtTs(t.completedAt) : undefined}>{fmtTime(t.completedAt)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/** Payouts for this ticket. Stripe stays in test mode, so every row says so. */
export function PaymentsTable({ payments, people }: { payments: readonly Payment[]; people: Map<bigint, Contributor> }) {
  if (payments.length === 0) return <Empty>No payouts yet. A contributor is paid only after the reviewer accepts their episode.</Empty>;
  const paid = payments.filter((p) => p.status === 'paid').reduce((n, p) => n + Number(p.amountCents), 0);
  const reserved = payments.filter((p) => p.status === 'pending' || p.status === 'awaiting_onboarding').reduce((n, p) => n + Number(p.amountCents), 0);
  return (
    <div>
      <p className="mb-2 flex flex-wrap items-center gap-2 text-sm tabular-nums text-zinc-500">
        {fmtCents(paid)} paid, {fmtCents(reserved)} reserved
      </p>
      <div className="overflow-x-auto rounded-md border border-zinc-200 dark:border-zinc-800">
        <table className="w-full text-left text-sm">
          <thead className="bg-zinc-50 text-xs text-zinc-500 dark:bg-zinc-900">
            <tr>
              <th className={th}>To</th>
              <th className={`${th} text-right`}>Amount</th>
              <th className={th}>For</th>
              <th className={th}>Status</th>
              <th className={th}>Stripe ref</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-100 dark:divide-zinc-800">
            {byNewest(payments).map((p) => {
              const who = people.get(p.contributorId);
              return (
                <tr key={p.id.toString()} className="align-top">
                  <td className={td}>{who?.name ?? `#${p.contributorId}`}</td>
                  <td className={`${td} text-right tabular-nums`}>{fmtCents(p.amountCents)}</td>
                  <td className={`${td} text-xs`}>
                    {p.episodeId ? (
                      <a href={`#episode-${p.episodeId}`} className="text-blue-600 hover:underline">episode #{p.episodeId.toString()}</a>
                    ) : p.batchRef ? (
                      `dataset batch ${p.batchRef}`
                    ) : (
                      'task'
                    )}
                  </td>
                  <td className={td}>
                    <StatusPill status={p.status} />
                    {p.error && <p className="mt-1 text-xs text-red-700 dark:text-red-400">{p.error}</p>}
                  </td>
                  <td className={`${td} text-xs text-zinc-500`}>
                    {p.providerRef ? <code>{p.providerRef}</code> : '—'}
                    <span className="ml-1 text-zinc-400">{p.provider === 'stripe_test' ? '(test)' : p.provider}</span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

