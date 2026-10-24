import Link from 'next/link';
import { Section } from '@/components/Section';
import { TicketList } from '@/components/TicketList';
import { EventLogList } from '@/components/EventLogList';
import { ContributorsPanel } from '@/components/ContributorsPanel';
import { JoinQr } from '@/components/JoinQr';

// TDM-111: the operator dashboard (it was `/` until the public landing page took that path).
export default function Dashboard() {
  return (
    <>
      <h1 className="gx-display-l mb-6">Tickets</h1>
      <Section title="Tickets (live)" aside={<Link href="/tickets/new" className="gx-btn gx-btn-primary gx-btn-sm ml-auto">File a ticket</Link>}>
        <TicketList />
      </Section>
      <Section title="Contributors (live)">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-start">
          <JoinQr />
          <div className="min-w-0 flex-1"><ContributorsPanel limit={10} /></div>
        </div>
      </Section>
      <Section title="Latest events (live)" aside={<Link href="/events" className="text-xs text-arc hover:underline">All events</Link>}>
        <EventLogList limit={10} />
      </Section>
    </>
  );
}
