import { TicketView } from '@/components/ticket/TicketView';
import { TicketAgentsPanel } from '@/components/agents/TicketAgentsPanel';

export default async function TicketPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return (
    <>
      <TicketView id={id} />
      {/* TDM-76: this ticket's droids and orchestrator agents, live. */}
      <TicketAgentsPanel ticketId={id} />
    </>
  );
}
