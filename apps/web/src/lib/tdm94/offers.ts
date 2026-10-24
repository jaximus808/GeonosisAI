// TDM-94: dataset-offer view model, shared by the ticket page's offer card and the seller page.
//
// Pure functions over live SpacetimeDB rows. The verdict maths mirror the TDM-14 reducers
// (dataset_offer.ts tdm14Verdict / tdm14Billable) and offers.py; the reducers stay the authority.
// QA rows come from TDM-92 (deterministic checks + the `deterministic_qa` summary) and TDM-93
// (`llm_relevance`, `llm_success` + the final review). TDM-93 also logs `qa.acceptance_rate`
// (suspect when it accepts nearly everything), `qa.llm_capped` and `qa.error` (outage, retried).

import type { RowOf } from '@/lib/spacetime/hooks';

export type Offer = RowOf<'datasetOffer'>;
export type Episode = RowOf<'episode'>;
export type QaRow = RowOf<'qaResult'>;
export type Review = RowOf<'review'>;
export type Payment = RowOf<'payment'>;
export type EventRow = RowOf<'eventLog'>;

type Json = Record<string, unknown>;

export const OPEN_STATUSES = ['proposed', 'accepted', 'ingesting', 'qa'];
export const INGESTABLE = ['accepted', 'ingesting', 'qa'];
export const SUMMARY_CHECK = 'deterministic_qa';
export const LLM_CHECKS = ['llm_relevance', 'llm_success'];

const CHECK_LABEL: Record<string, string> = {
  deterministic_qa: 'Deterministic checks',
  llm_relevance: 'Right task (LLM)',
  llm_success: 'Task succeeded (LLM)',
  actions: 'Actions file',
  duration: 'Duration',
  frame_drops: 'Frame drops',
  jerk: 'Smoothness (jerk)',
  embodiment: 'Embodiment',
  fps: 'Frame rate',
  camera_set: 'Cameras',
  action_range: 'Action range',
  static: 'Arm moves',
  duplicate: 'Not a duplicate',
};
export const checkLabel = (name: string) => CHECK_LABEL[name] ?? name.replace(/_/g, ' ');

export function parseJson(text: string | undefined | null): Json | null {
  if (!text) return null;
  try {
    const v = JSON.parse(text);
    return v && typeof v === 'object' && !Array.isArray(v) ? (v as Json) : null;
  } catch {
    return null;
  }
}

const sameId = (a: unknown, b: bigint) => a != null && String(a) === b.toString();

// ---------------------------------------------------------------- per episode

export type QaCheck = { name: string; label: string; passed: boolean; reason: string; detail: Json | null; row: QaRow };

/** Latest qa_result per check_name (highest id wins, as in TDM-14). */
export function latestChecks(rows: readonly QaRow[]): QaCheck[] {
  const latest = new Map<string, QaRow>();
  for (const r of rows) {
    const prev = latest.get(r.checkName);
    if (!prev || r.id > prev.id) latest.set(r.checkName, r);
  }
  return [...latest.values()]
    .map((row) => {
      const detail = parseJson(row.detailJson);
      const reason = typeof detail?.reason === 'string' ? detail.reason : '';
      return { name: row.checkName, label: checkLabel(row.checkName), passed: row.passed, reason, detail, row };
    })
    .sort((a, b) => order(a.name) - order(b.name) || a.name.localeCompare(b.name));
}
function order(name: string) {
  if (name === SUMMARY_CHECK) return 0;
  if (LLM_CHECKS.includes(name)) return 1 + LLM_CHECKS.indexOf(name);
  return 10;
}

export type Verdict = 'accepted' | 'rejected' | 'pending';

/** Where a pending episode is in the QA pipeline. */
export type QaStage =
  | 'deterministic' // waiting for TDM-92
  | 'llm' // deterministic passed, waiting for TDM-93's Claude call
  | 'capped' // TDM-93 hit the per-offer call cap: paused
  | 'retrying' // an outage (qa.error); stays pending and is retried
  | 'done';

export type EpisodeView = {
  episode: Episode;
  ref: string;
  verdict: Verdict;
  billable: boolean;
  checks: QaCheck[];
  failed: QaCheck[];
  review: Review | undefined;
  stage: QaStage;
  stageNote: string; // e.g. the outage text, the cap numbers
  reasons: string[]; // the human-readable why, failures first
};

/** Seller's folder name, e.g. .../purchased/3/ep_0007 -> ep_0007. */
export function episodeRef(e: Episode): string {
  return e.storageUri.replace(/\/+$/, '').split('/').pop() || `episode ${e.id}`;
}

export function episodeView(e: Episode, qa: readonly QaRow[], reviews: readonly Review[], events: readonly EventRow[]): EpisodeView {
  const checks = latestChecks(qa);
  const failed = checks.filter((c) => !c.passed);
  let review: Review | undefined;
  for (const r of reviews) if (!review || r.id > review.id) review = r;

  let verdict: Verdict;
  if (e.reviewStatus === 'rejected' || failed.length > 0) verdict = 'rejected';
  else if (review) verdict = review.accepted ? 'accepted' : 'rejected';
  else verdict = e.reviewStatus === 'accepted' ? 'accepted' : 'pending';
  const billable = verdict === 'accepted' && !!review?.accepted && checks.length > 0;

  let stage: QaStage = 'done';
  let stageNote = '';
  if (verdict === 'pending') {
    const summary = checks.find((c) => c.name === SUMMARY_CHECK);
    stage = summary?.passed ? 'llm' : 'deterministic';
    // Newest qa.* event for this episode that is newer than its newest QA row decides paused/retrying.
    const lastQaMicros = qa.reduce((m, r) => (r.createdAt.microsSinceUnixEpoch > m ? r.createdAt.microsSinceUnixEpoch : m), 0n);
    let last: { type: string; p: Json; at: bigint } | undefined;
    for (const ev of events) {
      if (ev.type !== 'qa.llm_capped' && ev.type !== 'qa.error') continue;
      const p = parseJson(ev.payloadJson);
      if (!p || !sameId(p.episode_id, e.id)) continue;
      const at = ev.ts.microsSinceUnixEpoch;
      if (at >= lastQaMicros && (!last || at > last.at)) last = { type: ev.type, p, at };
    }
    if (last?.type === 'qa.llm_capped') {
      stage = 'capped';
      stageNote = `${last.p.calls ?? '?'} of ${last.p.cap ?? '?'} LLM calls used for this offer`;
    } else if (last?.type === 'qa.error') {
      stage = 'retrying';
      stageNote = typeof last.p.error === 'string' ? last.p.error : 'QA could not run';
    }
  }

  const reasons: string[] = [];
  // The specific failing check first; the deterministic_qa summary only repeats it.
  const byDetail = [...failed].sort((a, b) => Number(a.name === SUMMARY_CHECK) - Number(b.name === SUMMARY_CHECK));
  for (const c of byDetail) reasons.push(c.reason ? `${c.label}: ${c.reason}` : `${c.label} failed`);
  if (review?.reason && !reasons.some((r) => r.includes(review!.reason))) reasons.push(review.reason);
  return { episode: e, ref: episodeRef(e), verdict, billable, checks, failed, review, stage, stageNote, reasons };
}

// ---------------------------------------------------------------- per offer

export type OfferMath = { target: number; existing: number; reserved: number } | null;

export type OfferView = {
  offer: Offer;
  episodes: EpisodeView[]; // oldest first
  live: number; // delivered and not rejected (holds a slot)
  accepted: number;
  billable: number;
  rejected: number;
  pending: number;
  openSlots: number;
  maxSpendCents: bigint;
  projectedPayoutCents: bigint;
  payments: Payment[];
  math: OfferMath;
  deliveryComplete: boolean;
  guard: { rate: number | null; reviewed: number; accepted: number; suspect: boolean; maxRate: number; minReviews: number } | null;
  capped: number; // episodes paused at the LLM call cap
  retrying: number; // episodes waiting out an outage
};

export function offerEvents(events: readonly EventRow[], offerId: bigint, episodeIds: Set<string>): EventRow[] {
  return events.filter((ev) => {
    if (!ev.payloadJson.includes('offer_id') && !ev.payloadJson.includes('episode_id')) return false;
    const p = parseJson(ev.payloadJson);
    if (!p) return false;
    return sameId(p.offer_id, offerId) || sameId(p.dataset_offer_id, offerId) || (p.episode_id != null && episodeIds.has(String(p.episode_id)));
  });
}

export function offerView(
  offer: Offer,
  allEpisodes: readonly Episode[],
  allQa: readonly QaRow[],
  allReviews: readonly Review[],
  allPayments: readonly Payment[],
  allEvents: readonly EventRow[],
): OfferView {
  const eps = allEpisodes
    .filter((e) => e.datasetOfferId === offer.id && e.kind === 'purchased')
    .sort((a, b) => (a.id < b.id ? -1 : 1));
  const ids = new Set(eps.map((e) => e.id.toString()));
  const events = offerEvents(allEvents, offer.id, ids);
  const qaBy = groupBy(allQa.filter((r) => ids.has(r.episodeId.toString())), (r) => r.episodeId.toString());
  const revBy = groupBy(allReviews.filter((r) => ids.has(r.episodeId.toString())), (r) => r.episodeId.toString());
  const views = eps.map((e) => episodeView(e, qaBy.get(e.id.toString()) ?? [], revBy.get(e.id.toString()) ?? [], events));

  const live = views.filter((v) => v.verdict !== 'rejected').length;
  const billable = views.filter((v) => v.billable).length;
  const price = offer.pricePerEpisodeCents;

  let math: OfferMath = null;
  let guard: OfferView['guard'] = null;
  let guardAt = -1n;
  let deliveryComplete = false;
  for (const ev of events) {
    const p = parseJson(ev.payloadJson);
    if (!p) continue;
    if (ev.type === 'dataset_offer.created' && p.math && typeof p.math === 'object') {
      const m = p.math as Json;
      math = { target: Number(m.target), existing: Math.max(Number(m.existing ?? 0), Number(m.dataset_accepted ?? 0)), reserved: Number(m.reserved_by_offers ?? 0) };
    } else if (ev.type === 'dataset_offer.delivery_complete') {
      deliveryComplete = true;
    } else if (ev.type === 'qa.acceptance_rate' && ev.ts.microsSinceUnixEpoch > guardAt) {
      guardAt = ev.ts.microsSinceUnixEpoch;
      guard = {
        rate: typeof p.rate === 'number' ? p.rate : null,
        reviewed: Number(p.reviewed ?? 0),
        accepted: Number(p.accepted ?? 0),
        suspect: p.suspect === true,
        maxRate: Number(p.max_rate ?? 0.95),
        minReviews: Number(p.min_reviews ?? 5),
      };
    }
  }

  return {
    offer,
    episodes: views,
    live,
    accepted: views.filter((v) => v.verdict === 'accepted').length,
    billable,
    rejected: views.filter((v) => v.verdict === 'rejected').length,
    pending: views.filter((v) => v.verdict === 'pending').length,
    openSlots: OPEN_STATUSES.includes(offer.status) ? Math.max(0, offer.neededCount - live) : 0,
    maxSpendCents: price * BigInt(offer.neededCount),
    projectedPayoutCents: offer.status === 'done' ? price * BigInt(offer.acceptedCount) : price * BigInt(billable),
    payments: allPayments.filter((p) => p.datasetOfferId === offer.id).sort((a, b) => (a.id < b.id ? -1 : 1)),
    math,
    deliveryComplete,
    guard,
    capped: views.filter((v) => v.stage === 'capped').length,
    retrying: views.filter((v) => v.stage === 'retrying').length,
  };
}

function groupBy<T>(rows: readonly T[], key: (r: T) => string): Map<string, T[]> {
  const m = new Map<string, T[]>();
  for (const r of rows) {
    const k = key(r);
    const list = m.get(k);
    if (list) list.push(r);
    else m.set(k, [r]);
  }
  return m;
}

/** "3 = 5 target − 2 already on the ticket − 0 held by other offers" */
export function neededArithmetic(v: OfferView): string {
  const n = v.offer.neededCount;
  if (!v.math) return `${n} needed (target episodes minus the ones the ticket already had)`;
  const parts = [`${n} = ${v.math.target} target − ${v.math.existing} already uploaded or accepted`];
  if (v.math.reserved > 0) parts.push(`− ${v.math.reserved} held by other open offers`);
  return parts.join(' ');
}

export const OFFER_STATUS_TEXT: Record<string, string> = {
  proposed: 'Waiting for the seller to accept',
  accepted: 'Accepted, waiting for uploads',
  ingesting: 'Receiving episodes',
  qa: 'Every slot delivered, under review',
  done: 'Settled',
  cancelled: 'Cancelled',
};

export const PAYMENT_STATUS_TEXT: Record<string, string> = {
  pending: 'Reserved, transfer queued',
  paid: 'Paid',
  failed: 'Transfer failed',
  awaiting_onboarding: 'Waiting for the seller to finish Stripe onboarding',
  blocked_budget: 'Held: over the ticket budget',
  reversed: 'Reversed',
};
