'use client';

import { useTicket } from '@/lib/spacetime/hooks';
import { fmtCents, fmtTs } from '@/lib/format';

export function TicketSummary({ id }: { id: string }) {
  const [ticket, ready] = useTicket(BigInt(id));
  if (!ready) return <p className="text-sm text-zinc-500">Loading ticket…</p>;
  if (!ticket) return <p className="text-sm text-zinc-500">No ticket #{id}.</p>;
  return (
    <dl className="grid grid-cols-2 gap-x-6 gap-y-2 rounded-lg border border-zinc-200 p-4 text-sm sm:grid-cols-4 dark:border-zinc-800">
      <div className="col-span-2 sm:col-span-4"><dt className="text-xs text-zinc-500">Task</dt><dd>{ticket.taskDescription}</dd></div>
      <div><dt className="text-xs text-zinc-500">Status</dt><dd className="font-mono">{ticket.status}</dd></div>
      <div><dt className="text-xs text-zinc-500">Target</dt><dd>{Math.round(ticket.targetSuccess * 100)}%</dd></div>
      <div><dt className="text-xs text-zinc-500">Spent / budget</dt><dd>{fmtCents(ticket.spentCents)} / {fmtCents(ticket.budgetCents)}</dd></div>
      <div><dt className="text-xs text-zinc-500">Created</dt><dd>{fmtTs(ticket.createdAt)}</dd></div>
    </dl>
  );
}
