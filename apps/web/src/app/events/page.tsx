import { Section } from '@/components/Section';
import { EventLogList } from '@/components/EventLogList';

export default function EventsPage() {
  return (
    <>
      <h1 className="mb-2 text-2xl font-semibold tracking-tight">Event log</h1>
      <p className="mb-6 text-sm text-zinc-500">Append-only and untouched: every row was written by a reducer call, never edited.</p>
      <Section title="event_log (live)">
        <EventLogList />
      </Section>
    </>
  );
}
