'use client';

import Link from 'next/link';
import { useEventLog } from '@/lib/eventlog/useEventLog';
import { EventRowButton } from '@/components/eventlog/EventRow';

/** Compact live event log (newest first), e.g. on the home page. Read-only. The full view is /events. */
export function EventLogList({ limit }: { limit?: number }) {
  const { ready, items, summary } = useEventLog();
  const shown = limit ? items.slice(0, limit) : items;

  if (!ready) return <p className="text-sm text-zinc-500">Subscribing to event_log…</p>;
  if (shown.length === 0) return <p className="text-sm text-zinc-500">No events yet.</p>;

  return (
    <div className="overflow-hidden rounded-lg border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900">
      <ol className="divide-y divide-zinc-100 dark:divide-zinc-800">
        {shown.map((it) => (
          <li key={it.id.toString()}>
            <EventRowButton item={it} />
          </li>
        ))}
      </ol>
      <p className="flex flex-wrap gap-x-3 border-t border-zinc-100 px-3 py-1.5 text-xs text-zinc-500 dark:border-zinc-800">
        <span>{summary.count} events, live</span>
        <span className="font-mono">chain {summary.hash.slice(0, 12)}</span>
        <Link href="/events" className="ml-auto text-teal-700 hover:underline dark:text-teal-400">
          Open the full log
        </Link>
      </p>
    </div>
  );
}
