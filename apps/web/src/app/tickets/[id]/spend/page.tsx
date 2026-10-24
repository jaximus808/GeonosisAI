import Link from 'next/link';
import { TicketSpendPanel } from '@/components/payments/TicketSpendPanel';
import { TestModeBand } from '@/components/payments/TestModeBand';

// TDM-64: standalone spend and budget page for one ticket. The ticket page can mount
// <TicketSpendPanel ticketId={id} /> directly instead.
export default async function TicketSpendPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!/^\d{1,18}$/.test(id)) {
    return <p className="text-sm text-zinc-500">Not a ticket id: {id}</p>;
  }
  return (
    <div className="mx-auto max-w-4xl">
      <TestModeBand detail="Budget, approvals and payouts below are Stripe test-mode amounts." />
      <p className="mt-6 text-sm text-zinc-500">
        <Link href={`/tickets/${id}`} className="hover:underline">
          Ticket #{id}
        </Link>
      </p>
      <TicketSpendPanel ticketId={BigInt(id)} className="mt-1" />
    </div>
  );
}
