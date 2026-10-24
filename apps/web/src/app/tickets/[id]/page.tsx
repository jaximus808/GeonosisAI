import { TicketView } from '@/components/ticket/TicketView';

export default async function TicketPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <TicketView id={id} />;
}
