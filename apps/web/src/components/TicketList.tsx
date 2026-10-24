'use client';

import Link from 'next/link';
import { useMemo } from 'react';
import { useRows } from '@/lib/spacetime/hooks';
import { fmtCents, fmtTs } from '@/lib/format';

/** Live ticket list, newest first. New tickets appear without a refresh. */
export function TicketList() {
  const [tickets, ready] = useRows('ticket');
  const [robots] = useRows('robot');
  const robotName = useMemo(() => new Map(robots.map((r) => [r.id, r.name])), [robots]);
  const sorted = useMemo(() => [...tickets].sort((a, b) => (a.id < b.id ? 1 : a.id > b.id ? -1 : 0)), [tickets]);

  if (!ready) return <p className="text-sm text-zinc-500">Subscribing to ticket…</p>;
  if (sorted.length === 0) {
    return (
      <p className="rounded-lg border border-dashed border-zinc-300 p-4 text-sm text-zinc-500 dark:border-zinc-700">
        No tickets yet. They show up here live as soon as one is created.
      </p>
    );
  }
  return (
    <div className="overflow-x-auto rounded-lg border border-zinc-200 dark:border-zinc-800">
      <table className="w-full text-left text-sm">
        <thead className="bg-zinc-50 text-xs uppercase text-zinc-500 dark:bg-zinc-900">
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
        <tbody className="divide-y divide-zinc-100 dark:divide-zinc-800">
          {sorted.map((t) => (
            <tr key={t.id.toString()} className="hover:bg-zinc-50 dark:hover:bg-zinc-900/50">
              <td className="px-3 py-2 font-mono text-xs text-zinc-400">{t.id.toString()}</td>
              <td className="px-3 py-2">
                <Link href={`/tickets/${t.id}`} className="hover:underline">{t.taskDescription}</Link>
              </td>
              <td className="px-3 py-2 text-xs">
                <Link href={`/robots/${t.robotId}`} className="hover:underline">{robotName.get(t.robotId) ?? `#${t.robotId}`}</Link>
              </td>
              <td className="px-3 py-2 font-mono text-xs">{t.status}</td>
              <td className="px-3 py-2 text-xs">{Math.round(t.targetSuccess * 100)}%</td>
              <td className="px-3 py-2 text-xs">{fmtCents(t.spentCents)} / {fmtCents(t.budgetCents)}</td>
              <td className="whitespace-nowrap px-3 py-2 text-xs text-zinc-500">{fmtTs(t.createdAt)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
