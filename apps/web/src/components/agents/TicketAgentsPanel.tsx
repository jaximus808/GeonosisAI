'use client';

// TDM-76: the agents panel embedded on the ticket page (only this ticket's agents).

import { AgentsBoard } from './AgentsBoard';

export function TicketAgentsPanel({ ticketId }: { ticketId: string }) {
  let tid: number;
  try {
    tid = Number(BigInt(ticketId));
  } catch {
    return null;
  }
  return (
    <section id="agents" className="mb-10 scroll-mt-6 border-t border-zinc-200 pt-8 dark:border-zinc-800">
      <div className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h2 className="text-lg font-semibold tracking-tight">Agents on this ticket</h2>
        <span className="text-sm text-zinc-500">ran on their own, no person involved</span>
        <a href="/agents" className="ml-auto text-sm text-zinc-500 underline-offset-2 hover:text-zinc-900 hover:underline dark:hover:text-zinc-100">
          All agents
        </a>
      </div>
      <AgentsBoard ticketId={tid} variant="ticket" />
    </section>
  );
}
