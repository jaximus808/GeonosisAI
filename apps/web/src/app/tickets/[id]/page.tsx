import { TicketView } from '@/components/ticket/TicketView';
import { TicketAgentsPanel } from '@/components/agents/TicketAgentsPanel';
import { Tdm104ScenePanel } from '@/components/scene/Tdm104ScenePanel';

export default async function TicketPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return (
    <>
      <TicketView id={id} />
      {/* TDM-104: the generated practice scene (3D preview, Deploy scene to the sim robot). */}
      <Tdm104ScenePanel ticketId={id} />
      {/* TDM-76: this ticket's droids and orchestrator agents, live. */}
      <TicketAgentsPanel ticketId={id} />
    </>
  );
}
