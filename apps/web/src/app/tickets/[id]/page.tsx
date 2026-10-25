import { TicketView } from '@/components/ticket/TicketView';

// TDM-116: the ticket cockpit. The practice scene (TDM-104) is in its Settings tab and the agents panel
// (TDM-76) in its Agents tab.
export default async function TicketPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <TicketView id={id} />;
}
