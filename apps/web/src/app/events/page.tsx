import { Suspense } from 'react';
import { EventLogExplorer } from '@/components/eventlog/EventLogExplorer';

export const metadata = { title: 'Event log · GeonosisAI' };

export default function EventsPage() {
  return (
    <>
      <h1 className="text-2xl font-semibold tracking-tight">Event log</h1>
      <p className="mt-1 mb-6 max-w-2xl text-sm text-zinc-600 dark:text-zinc-400">
        Everything the factory did, in order.
      </p>
      <Suspense fallback={<p className="text-sm text-zinc-500">Loading…</p>}>
        <EventLogExplorer />
      </Suspense>
    </>
  );
}
