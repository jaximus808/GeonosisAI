'use client';

import Link from 'next/link';
import { useMemo } from 'react';
import { useRows } from '@/lib/spacetime/hooks';
import { fmtCents, fmtTs } from '@/lib/format';
import { StatusTag } from '@/components/brand/StatusTag';

/** Live ticket list, newest first. New tickets appear without a refresh. */
export function TicketList() {
  const [tickets, ready] = useRows('ticket');
  const [robots] = useRows('robot');
  const robotName = useMemo(() => new Map(robots.map((r) => [r.id, r.name])), [robots]);
  const sorted = useMemo(() => [...tickets].sort((a, b) => (a.id < b.id ? 1 : a.id > b.id ? -1 : 0)), [tickets]);

  if (!ready) return <p className="gx-small">Subscribing to tickets…</p>;
  if (sorted.length === 0) {
    return (
      <p className="rounded-sm border border-dashed border-line-strong p-4 text-sm text-ink-muted">
        No tickets yet. File one to put a robot on the line.
      </p>
    );
  }
  return (
    <div className="overflow-x-auto rounded-sm border border-line bg-surface">
      <table className="w-full text-left text-sm">
        <thead className="gx-label text-ink-muted">
          <tr>
            <th className="px-3 py-2">#</th>
            <th className="px-3 py-2">Task</th>
            <th className="px-3 py-2">Robot</th>
            <th className="px-3 py-2">Status</th>
            <th className="px-3 py-2">Target</th>
            <th className="px-3 py-2">Spent / budget</th>
            <th className="px-3 py-2">Created</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-line border-t border-line">
          {sorted.map((t) => (
            <tr key={t.id.toString()} className="transition-colors hover:bg-surface-sunken">
              <td className="gx-id px-3 py-2">TKT-{t.id.toString().padStart(4, '0')}</td>
              <td className="px-3 py-2">
                <Link href={`/tickets/${t.id}`} className="hover:underline">{t.taskDescription}</Link>
              </td>
              <td className="px-3 py-2 font-mono text-xs">
                <Link href={`/robots/${t.robotId}`} className="hover:underline">{robotName.get(t.robotId) ?? `#${t.robotId}`}</Link>
              </td>
              <td className="px-3 py-2"><StatusTag status={t.status} /></td>
              <td className="gx-data px-3 py-2">{Math.round(t.targetSuccess * 100)}%</td>
              <td className="gx-data px-3 py-2"><span className="text-ochre">{fmtCents(t.spentCents)}</span> of {fmtCents(t.budgetCents)}</td>
              <td className="gx-small whitespace-nowrap px-3 py-2">{fmtTs(t.createdAt)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
