import Link from 'next/link';
import { Section } from '@/components/Section';
import { TicketList } from '@/components/TicketList';
import { EventLogList } from '@/components/EventLogList';

export default function Home() {
  return (
    <>
      <h1 className="mb-6 text-2xl font-semibold tracking-tight">Tickets</h1>
      <Section title="Tickets (live)" aside={<Link href="/tickets/new" className="text-xs text-blue-600 hover:underline">New ticket</Link>}>
        <TicketList />
      </Section>
      <Section title="Latest events (live)" aside={<Link href="/events" className="text-xs text-blue-600 hover:underline">All events</Link>}>
        <EventLogList limit={10} />
      </Section>
    </>
  );
}
