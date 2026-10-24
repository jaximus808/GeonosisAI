'use client';

import { fmtTs } from '@/lib/format';
import { Line, gxTicketStations } from '@/components/brand/Line';
import { fmtTime, type Ticket, type TicketEvent } from './util';

/**
 * The line (TDM-109 brand): the ticket's stations as rivets on a track, Baseline to Ship. Done stations
 * are ink, the running one melt, upcoming hollow steel, the station a failed ticket stopped at slag.
 * Each station's note is a real number from the ticket's rows (`notes`), else the time it was last
 * entered (from event_log). An eval loop-back resets the later stations to upcoming (one line, never two).
 */
export function TimelineStrip({ ticket, events, notes = {} }: { ticket: Ticket; events: TicketEvent[]; notes?: Partial<Record<string, string>> }) {
  const entered = new Map<string, { ts: TicketEvent['row']['ts']; iteration: number; note?: string }[]>();
  let ended: { to: string; ts: TicketEvent['row']['ts']; note?: string; from?: string } | undefined;
  let lastActive: string | undefined;
  for (const { row, payload } of events) {
    if (row.type !== 'ticket.status_changed') continue;
    const to = String(payload.to ?? '');
    const note = typeof payload.note === 'string' && payload.note ? payload.note : undefined;
    if (to === 'failed' || to === 'cancelled') ended = { to, ts: row.ts, note, from: typeof payload.from === 'string' ? payload.from : lastActive };
    else lastActive = to;
    entered.set(to, [...(entered.get(to) ?? []), { ts: row.ts, iteration: Number(payload.iteration ?? 0), note }]);
  }

  const timeNotes: Record<string, string> = {};
  for (const [k, hits] of entered) {
    const last = hits[hits.length - 1];
    timeNotes[k] = `${fmtTime(last.ts)}${hits.length > 1 ? ` ×${hits.length}` : ''}`;
  }
  const stations = gxTicketStations(ticket.status, {
    notes: { ...timeNotes, ...notes, shipped: notes.shipped ?? timeNotes.shipped },
    stoppedAt: ended?.from,
  });
  const queued = ticket.status === 'created' || ticket.status === 'onboarding';

  return (
    <section className="gx-plate mb-6" aria-label="Ticket stages" data-testid="ticket-line">
      <Line stations={stations} />
      {queued && <p className="gx-small mt-3 mb-0">Queued since {fmtTs(ticket.createdAt)}. The baseline starts once the robot is onboarded.</p>}
      {ended && (
        <p className="mt-3 mb-0 text-sm text-slag">
          {ended.to === 'failed' ? 'Failed' : 'Cancelled'} at {fmtTs(ended.ts)}
          {ended.note ? `: ${ended.note}` : ''}
        </p>
      )}
      {ticket.iteration > 0 && (
        <p className="gx-small mt-2 mb-0">
          Looped back {ticket.iteration} time{ticket.iteration === 1 ? '' : 's'} after an eval missed the target. A × count next to a station shows how often it ran.
        </p>
      )}
    </section>
  );
}
