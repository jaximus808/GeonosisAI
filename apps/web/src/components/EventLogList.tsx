'use client';

import { useMemo } from 'react';
import { useRows } from '@/lib/spacetime/hooks';
import { fmtTs } from '@/lib/format';

/** Live, append-only event log (newest first). Read-only: rows are never edited. */
export function EventLogList({ limit }: { limit?: number }) {
  const [rows, ready] = useRows('eventLog');
  const sorted = useMemo(() => {
    const s = [...rows].sort((a, b) => (a.id < b.id ? 1 : a.id > b.id ? -1 : 0));
    return limit ? s.slice(0, limit) : s;
  }, [rows, limit]);

  if (!ready) return <p className="text-sm text-zinc-500">Subscribing to event_log…</p>;
  if (sorted.length === 0) return <p className="text-sm text-zinc-500">No events yet.</p>;

  return (
    <div className="overflow-x-auto rounded-lg border border-zinc-200 dark:border-zinc-800">
      <table className="w-full text-left text-sm">
        <thead className="bg-zinc-50 text-xs uppercase text-zinc-500 dark:bg-zinc-900">
          <tr>
            <th className="px-3 py-2">#</th>
            <th className="px-3 py-2">Time</th>
            <th className="px-3 py-2">Actor</th>
            <th className="px-3 py-2">Type</th>
            <th className="px-3 py-2">Payload</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-zinc-100 dark:divide-zinc-800">
          {sorted.map((e) => (
            <tr key={e.id.toString()} className="align-top">
              <td className="px-3 py-2 font-mono text-xs text-zinc-400">{e.id.toString()}</td>
              <td className="whitespace-nowrap px-3 py-2 text-xs text-zinc-500">{fmtTs(e.ts)}</td>
              <td className="px-3 py-2">{e.actor}</td>
              <td className="px-3 py-2 font-mono text-xs">{e.type}</td>
              <td className="max-w-md truncate px-3 py-2 font-mono text-xs text-zinc-500" title={e.payloadJson}>
                {e.payloadJson}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="border-t border-zinc-100 px-3 py-1.5 text-xs text-zinc-400 dark:border-zinc-800">
        {rows.length} events total, live
      </p>
    </div>
  );
}
