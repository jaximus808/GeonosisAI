'use client';

import { fmtCents } from '@/lib/format';
import { StatusPill, TestModeBadge } from './Badges';
import { byNewest, type Contributor, type DatasetOffer } from './util';

/** E09 slot: offers to buy episodes from a dataset seller. Renders nothing until an offer exists. */
export function OfferCard({ offers, people }: { offers: readonly DatasetOffer[]; people: Map<bigint, Contributor> }) {
  if (offers.length === 0) return null;
  return (
    <ul className="space-y-3">
      {byNewest(offers).map((o) => {
        const seller = people.get(o.sellerId);
        const need = Math.max(1, o.neededCount);
        return (
          <li key={o.id.toString()} className="rounded-lg border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <h3 className="font-semibold">{seller?.name ?? `Seller #${o.sellerId}`}</h3>
              <StatusPill status={o.status} />
              <TestModeBadge />
              <span className="ml-auto text-sm tabular-nums">
                {fmtCents(o.pricePerEpisodeCents)} per accepted episode, up to {fmtCents(o.pricePerEpisodeCents * BigInt(o.neededCount))}
              </span>
            </div>
            <div className="mt-3 h-2 overflow-hidden rounded-full bg-zinc-200 dark:bg-zinc-800" role="img" aria-label={`${o.acceptedCount} accepted, ${o.ingestedCount} ingested of ${o.neededCount}`}>
              <div className="relative h-full">
                <div className="absolute inset-y-0 left-0 bg-zinc-400/70" style={{ width: `${Math.min(100, (o.ingestedCount / need) * 100)}%` }} />
                <div className="absolute inset-y-0 left-0 bg-emerald-600" style={{ width: `${Math.min(100, (o.acceptedCount / need) * 100)}%` }} />
              </div>
            </div>
            <p className="mt-1.5 text-xs tabular-nums text-zinc-500">
              {o.acceptedCount} accepted, {o.ingestedCount} ingested, {o.neededCount} needed
              {o.sourceUri && <> from <code>{o.sourceUri}</code></>}
            </p>
          </li>
        );
      })}
    </ul>
  );
}
