'use client';

import Link from 'next/link';
import { fmtCents } from '@/lib/format';
import { EpisodeVerdicts, OfferCounts, PayoutStatus, QaFlags, SlotRail } from '@/components/offers/OfferParts';
import { neededArithmetic, OFFER_STATUS_TEXT, type OfferView } from '@/lib/tdm94/offers';
import { useTicketOfferViews } from '@/lib/tdm94/useOfferLive';
import { StatusPill } from './Badges';
import type { Contributor, DatasetOffer } from './util';

/**
 * E09 dataset offers on the ticket page (TDM-75 slot, filled by TDM-94). Renders nothing until an
 * offer exists. Live: slots fill as the seller delivers, verdicts and QA reasons land per episode,
 * the payout follows the settle step. Same props as the TDM-75 slot, so TicketView is unchanged.
 */
export function OfferCard({ offers, people }: { offers: readonly DatasetOffer[]; people: Map<bigint, Contributor> }) {
  const ticketId = offers[0]?.ticketId ?? 0n;
  const views = useTicketOfferViews(ticketId, offers);
  if (offers.length === 0) return null;
  return (
    <ul className="space-y-4">
      {views.map((v) => (
        <OfferItem key={v.offer.id.toString()} view={v} sellerName={people.get(v.offer.sellerId)?.name} />
      ))}
    </ul>
  );
}

function OfferItem({ view: v, sellerName }: { view: OfferView; sellerName?: string }) {
  const o = v.offer;
  return (
    <li className="rounded-lg border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <h3 className="font-semibold">
          Offer {o.id.toString()} to {sellerName ?? `seller #${o.sellerId}`}
        </h3>
        <StatusPill status={o.status} />
        <span className="text-sm text-zinc-500">{OFFER_STATUS_TEXT[o.status] ?? ''}</span>
        <Link href={`/sell/${o.id}`} className="ml-auto text-sm font-medium text-blue-700 hover:underline dark:text-blue-400">
          Seller page
        </Link>
      </div>

      <div className="mt-3 grid gap-x-6 gap-y-1 text-sm sm:grid-cols-[auto_1fr]">
        <span className="text-zinc-500">Buying</span>
        <span className="tabular-nums">
          <strong>{o.neededCount}</strong> episode{o.neededCount === 1 ? '' : 's'}: {neededArithmetic(v)}
        </span>
        <span className="text-zinc-500">Price</span>
        <span className="flex flex-wrap items-center gap-2 tabular-nums">
          {fmtCents(o.pricePerEpisodeCents)} per accepted episode, at most {fmtCents(v.maxSpendCents)}
        </span>
        <span className="text-zinc-500">Slots</span>
        <span className="tabular-nums">
          {v.live} of {o.neededCount} held, {v.openSlots} open
          {o.sourceUri && (
            <>
              {' '}from <code className="text-xs">{o.sourceUri}</code>
            </>
          )}
        </span>
      </div>

      <div className="mt-3">
        <SlotRail view={v} size="sm" />
      </div>
      <div className="mt-3">
        <OfferCounts view={v} />
      </div>
      <div className="mt-3">
        <QaFlags view={v} />
      </div>
      <div className="mt-3">
        <EpisodeVerdicts view={v} priceCents={o.pricePerEpisodeCents} />
      </div>
      <div className="mt-3 border-t border-zinc-200 pt-3 dark:border-zinc-800">
        <PayoutStatus view={v} />
      </div>
    </li>
  );
}
