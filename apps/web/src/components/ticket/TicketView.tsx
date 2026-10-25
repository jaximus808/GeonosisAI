'use client';

import { TicketCockpit } from '@/components/cockpit/TicketCockpit';

/**
 * TDM-75 -> TDM-116: the live ticket page. It used to be one long column of plates; it is now the ticket
 * cockpit (components/cockpit): a top bar with the line, a rail of tabs and a main stage that follows the
 * ticket's current station. Every view is a SpacetimeDB subscription; nothing is mocked.
 */
export function TicketView({ id }: { id: string }) {
  return <TicketCockpit id={id} />;
}
