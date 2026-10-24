'use client';

// TDM-94: live rows for the offers of one ticket (offer card) or one offer (seller page).
// Every input is a SpacetimeDB subscription, so verdicts, QA reasons and payouts update as they land.

import { useMemo } from 'react';
import { useRows } from '@/lib/spacetime/hooks';
import { offerView, type Offer, type OfferView } from './offers';

/** Views for the given offers of one ticket (newest offer first). */
export function useTicketOfferViews(ticketId: bigint, offers: readonly Offer[]): OfferView[] {
  const [episodes] = useRows('episode', (r) => r.ticketId.eq(ticketId));
  const [reviews] = useRows('review', (r) => r.ticketId.eq(ticketId));
  const [payments] = useRows('payment', (r) => r.ticketId.eq(ticketId));
  // qa_result has no ticket column; filtered to this ticket's purchased episodes in offerView.
  const [qa] = useRows('qaResult');
  const [events] = useRows('eventLog');
  return useMemo(
    () =>
      [...offers]
        .sort((a, b) => (a.id < b.id ? 1 : -1))
        .map((o) => offerView(o, episodes, qa, reviews, payments, events)),
    [offers, episodes, qa, reviews, payments, events],
  );
}

/** One offer by id, live. Returns [view | undefined, ready]. */
export function useOfferLive(offerId: bigint): readonly [OfferView | undefined, boolean, Offer | undefined] {
  const [offers, ready] = useRows('datasetOffer', (r) => r.id.eq(offerId));
  const offer = offers[0];
  const views = useTicketOfferViews(offer?.ticketId ?? 0n, offers);
  return [offer ? views[0] : undefined, ready, offer] as const;
}
