'use client';

import type { Timestamp } from 'spacetimedb';
import { fmtTs } from '@/lib/format';
import { STAGES, fmtTime, type Ticket, type TicketEvent } from './util';

type Entry = { ts: Timestamp; iteration: number; note?: string };

/** The flywheel stages in order, each with the last time the ticket entered it (from event_log). */
export function TimelineStrip({ ticket, events }: { ticket: Ticket; events: TicketEvent[] }) {
  const entered = new Map<string, Entry[]>();
  const push = (k: string, e: Entry) => entered.set(k, [...(entered.get(k) ?? []), e]);
  push('created', { ts: ticket.createdAt, iteration: 0 });
  let skippedOnboarding = false;
  let ended: { to: string; ts: Entry['ts']; note?: string } | undefined;
  for (const { row, payload } of events) {
    if (row.type !== 'ticket.status_changed') continue;
    const to = String(payload.to ?? '');
    const note = typeof payload.note === 'string' && payload.note ? payload.note : undefined;
    if (payload.from === 'created' && to === 'baseline') skippedOnboarding = true;
    if (to === 'failed' || to === 'cancelled') ended = { to, ts: row.ts, note };
    push(to, { ts: row.ts, iteration: Number(payload.iteration ?? 0), note });
  }

  const current = ticket.status;
  const curIdx = STAGES.findIndex((s) => s.key === current);
  // A failed/cancelled ticket stops at the last real stage it reached.
  const lastReal = curIdx >= 0 ? curIdx : Math.max(...STAGES.map((s, i) => (entered.has(s.key) ? i : -1)));

  return (
    <div className="mb-10">
      <ol className="grid grid-cols-5 gap-y-4 sm:grid-cols-10" aria-label="Ticket stages">
        {STAGES.map((s, i) => {
          const hits = entered.get(s.key) ?? [];
          const last = hits[hits.length - 1];
          const isCur = s.key === current;
          const done = curIdx < 0 ? i <= lastReal : i < curIdx;
          const skipped = s.key === 'onboarding' && skippedOnboarding && hits.length === 0;
          const dot = skipped
            ? 'border border-dashed border-zinc-400 bg-transparent'
            : isCur && s.key === 'shipped'
            ? 'bg-emerald-600 ring-4 ring-emerald-600/20'
            : isCur
            ? 'bg-sky-600 ring-4 ring-sky-600/20'
            : done
              ? 'bg-zinc-900 dark:bg-zinc-100'
              : 'bg-white ring-1 ring-zinc-300 dark:bg-zinc-950 dark:ring-zinc-700';
          const line = i < lastReal ? 'bg-zinc-900 dark:bg-zinc-100' : 'bg-zinc-200 dark:bg-zinc-800';
          return (
            <li key={s.key} className="relative pr-2" aria-current={isCur ? 'step' : undefined}>
              <div className="flex items-center">
                <span className={`relative z-10 h-3 w-3 shrink-0 rounded-full ${dot}`} />
                {i < STAGES.length - 1 && <span className={`h-0.5 flex-1 ${line}`} />}
              </div>
              <p className={`mt-2 text-sm ${isCur ? 'font-semibold text-sky-700 dark:text-sky-400' : done ? '' : 'text-zinc-400'}`}>
                {s.label}
              </p>
              <p
                className="text-xs tabular-nums text-zinc-500"
                title={hits.map((h) => `${fmtTs(h.ts)} (iteration ${h.iteration})${h.note ? `: ${h.note}` : ''}`).join('\n') || undefined}
              >
                {skipped ? 'skipped' : last ? fmtTime(last.ts) : ''}
                {hits.length > 1 && <span className="ml-1 text-zinc-400">×{hits.length}</span>}
              </p>
            </li>
          );
        })}
      </ol>
      {ended && (
        <p className="mt-3 text-sm text-red-700 dark:text-red-400">
          {ended.to === 'failed' ? 'Failed' : 'Cancelled'} at {fmtTs(ended.ts)}
          {ended.note ? `: ${ended.note}` : ''}
        </p>
      )}
      {ticket.iteration > 0 && (
        <p className="mt-2 text-xs text-zinc-500">
          Looped back {ticket.iteration} time{ticket.iteration === 1 ? '' : 's'} after an eval missed the target; the count next to a stage shows how often it ran.
        </p>
      )}
    </div>
  );
}
