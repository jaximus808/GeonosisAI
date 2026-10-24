import Link from 'next/link';
import { Placeholder, Section } from '@/components/Section';
import { TicketSummary } from '@/components/TicketSummary';

export default async function TicketPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return (
    <>
      <h1 className="mb-2 text-2xl font-semibold tracking-tight">Ticket #{id}</h1>
      <Link href={`/tickets/${id}/report`} className="mb-6 inline-block text-sm text-blue-600 hover:underline">Report</Link>
      <Section title="Ticket (live)"><TicketSummary id={id} /></Section>
      <Placeholder owner="E11 live ticket view">Data plan, failure clusters, tasks, episodes, reviews, training and eval runs.</Placeholder>
    </>
  );
}
