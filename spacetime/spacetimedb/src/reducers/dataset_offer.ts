// TDM-14: dataset-offer and QA reducers, with the server-enforced N - existing cap.
//
// The user's rule: the factory pays a dataset seller only for
//   target_episodes - (episodes already uploaded or accepted for this ticket)
// and only for purchased episodes that QA accepted (deterministic qa_result
// checks AND the reviewer's review row).
//
// Race safety: SpacetimeDB reducers run as serializable transactions, so every
// check-then-write below is atomic. On top of that, open offers RESERVE their
// unfilled slots, so two offers created back to back for the same ticket can
// never promise more than target_episodes in total:
//
//   committed(ticket) = live training episodes for the ticket
//                     + sum over open offers of (needed_count - live episodes of that offer)
//   needed_count      = max(0, target_episodes - committed)    at create time
//   ingest allowed    iff live(offer) < needed_count
//                     and committed(ticket) < target_episodes   (re-checked live)
//
// "live" = an episode of a training kind that is not rejected (pending or accepted).
// A rejected purchased episode frees its slot, so the seller may send a replacement.
// ingested_count counts every delivery (including rejected ones), so it can exceed
// needed_count only by the number of rejected deliveries.
import { t, SenderError } from 'spacetimedb/server';
import spacetimedb, { type RdfCtx } from '../schema';
import { requireRole, serviceRole } from '../auth';
import { DEFAULT_RUN_SETTINGS } from '../enums';

// Episode kinds that go into the training dataset (baseline/eval rollouts do not).
const TDM14_TRAINING_KINDS = ['clean_teleop', 'recovery_takeover', 'purchased'];
// Offer statuses that still hold reserved slots.
const TDM14_OPEN_OFFER = ['proposed', 'accepted', 'ingesting', 'qa'];
// Statuses in which a delivery may be ingested.
const TDM14_INGESTABLE = ['accepted', 'ingesting', 'qa'];
// Ticket statuses that no longer buy data.
const TDM14_CLOSED_TICKET = ['shipped', 'failed', 'cancelled'];

type Tdm14Verdict = 'accepted' | 'rejected' | 'pending';

interface Tdm14QaSummary {
  checks: number; // distinct check names recorded
  failed: string[]; // check names whose latest result failed
}

function tdm14QaSummary(ctx: RdfCtx, episodeId: bigint): Tdm14QaSummary {
  // Latest result per check_name wins (highest id; ids are monotonic within the DB).
  const latest = new Map<string, { id: bigint; passed: boolean }>();
  for (const r of ctx.db.qaResult.episode_id.filter(episodeId)) {
    const prev = latest.get(r.check_name);
    if (!prev || r.id > prev.id) latest.set(r.check_name, { id: r.id, passed: r.passed });
  }
  const failed = [...latest.entries()].filter(([, v]) => !v.passed).map(([k]) => k);
  return { checks: latest.size, failed };
}

/** Latest review row for the episode, if any. */
function tdm14LatestReview(ctx: RdfCtx, episodeId: bigint) {
  let best: { id: bigint; accepted: boolean } | undefined;
  for (const r of ctx.db.review.episode_id.filter(episodeId)) {
    if (!best || r.id > best.id) best = { id: r.id, accepted: r.accepted };
  }
  return best;
}

/**
 * Verdict from QA rows, review rows and episode.review_status (whichever is
 * strictest). A failed deterministic check rejects regardless of the review.
 */
function tdm14Verdict(ctx: RdfCtx, ep: { id: bigint; review_status: string }): Tdm14Verdict {
  if (ep.review_status === 'rejected') return 'rejected';
  if (tdm14QaSummary(ctx, ep.id).failed.length > 0) return 'rejected';
  const rev = tdm14LatestReview(ctx, ep.id);
  if (rev) return rev.accepted ? 'accepted' : 'rejected';
  if (ep.review_status === 'accepted') return 'accepted';
  return 'pending';
}

/** Counted toward the deliverable for payout: both QA layers accepted it. */
function tdm14Billable(ctx: RdfCtx, ep: { id: bigint; review_status: string }): boolean {
  if (tdm14Verdict(ctx, ep) !== 'accepted') return false;
  if (!tdm14LatestReview(ctx, ep.id)?.accepted) return false; // LLM/reviewer verdict required
  return tdm14QaSummary(ctx, ep.id).checks > 0; // deterministic checks required
}

function tdm14OfferEpisodes(ctx: RdfCtx, ticketId: bigint, offerId: bigint) {
  return [...ctx.db.episode.ticket_id.filter(ticketId)].filter(
    (e) => e.dataset_offer_id === offerId && e.kind === 'purchased'
  );
}

function tdm14LiveCount(ctx: RdfCtx, eps: { id: bigint; review_status: string }[]): number {
  return eps.filter((e) => tdm14Verdict(ctx, e) !== 'rejected').length;
}

function tdm14Target(ctx: RdfCtx, ticketId: bigint): { target: number; source: string } {
  const rs = ctx.db.runSettings.ticket_id.find(ticketId);
  if (rs) return { target: rs.target_episodes, source: 'run_settings' };
  return { target: DEFAULT_RUN_SETTINGS.target_episodes, source: 'default' };
}

interface Tdm14Commitment {
  target: number;
  target_source: string;
  existing: number; // live training episodes (uploaded or accepted) for the ticket
  dataset_accepted: number; // dataset.accepted_count, as a floor for `existing`
  reserved_by_offers: number; // unfilled slots held by open offers (excluding `excludeOfferId`)
  committed: number; // max(existing, dataset_accepted) + reserved_by_offers
}

/** The N - existing math for a ticket. excludeOfferId leaves one offer's reservation out. */
function tdm14Commitment(ctx: RdfCtx, ticketId: bigint, excludeOfferId: bigint): Tdm14Commitment {
  const { target, source } = tdm14Target(ctx, ticketId);
  const eps = [...ctx.db.episode.ticket_id.filter(ticketId)].filter((e) =>
    TDM14_TRAINING_KINDS.includes(e.kind)
  );
  const existing = tdm14LiveCount(ctx, eps);
  const datasetAccepted = ctx.db.dataset.ticket_id.find(ticketId)?.accepted_count ?? 0;
  let reserved = 0;
  for (const o of ctx.db.datasetOffer.ticket_id.filter(ticketId)) {
    if (o.id === excludeOfferId || !TDM14_OPEN_OFFER.includes(o.status)) continue;
    const live = tdm14LiveCount(
      ctx,
      eps.filter((e) => e.dataset_offer_id === o.id && e.kind === 'purchased')
    );
    reserved += Math.max(0, o.needed_count - live);
  }
  return {
    target,
    target_source: source,
    existing,
    dataset_accepted: datasetAccepted,
    reserved_by_offers: reserved,
    committed: Math.max(existing, datasetAccepted) + reserved,
  };
}

function tdm14Log(ctx: RdfCtx, actor: string, type: string, payload: Record<string, unknown>) {
  ctx.db.eventLog.insert({
    id: 0n,
    ts: ctx.timestamp,
    actor,
    type,
    payload_json: JSON.stringify(payload, (_k, v) => (typeof v === 'bigint' ? v.toString() : v)),
  });
}

function tdm14Offer(ctx: RdfCtx, offerId: bigint) {
  const offer = ctx.db.datasetOffer.id.find(offerId);
  if (!offer) throw new SenderError(`dataset_offer ${offerId} not found`);
  return offer;
}

/** Service role from the allow-list, or the seller acting as themselves. */
function tdm14RequireServiceOrSeller(ctx: RdfCtx, sellerId: bigint, roles: string[]): string {
  const role = serviceRole(ctx);
  if (role !== undefined) {
    requireRole(ctx, ...roles);
    return role;
  }
  const seller = ctx.db.contributor.id.find(sellerId);
  if (seller?.identity && seller.identity.equals(ctx.sender)) return 'seller';
  throw new SenderError(
    `unauthorized: needs role ${roles.join('|')} or the seller's own identity`
  );
}

function tdm14ParseJson(field: string, value: string) {
  try {
    JSON.parse(value);
  } catch {
    throw new SenderError(`${field} must be valid JSON`);
  }
}

// ---------------------------------------------------------------- reducers

/**
 * The factory offers to buy episodes from a dataset seller for a ticket.
 * needed_count = max(0, target_episodes - existing - reserved by other open
 * offers); rejected when that is 0.
 */
export const create_dataset_offer = spacetimedb.reducer(
  {
    ticket_id: t.u64(),
    seller_id: t.u64(),
    price_per_episode_cents: t.u64(),
    source_uri: t.option(t.string()),
  },
  (ctx, { ticket_id, seller_id, price_per_episode_cents, source_uri }) => {
    const role = requireRole(ctx, 'orchestrator', 'mcp', 'web');
    const ticket = ctx.db.ticket.id.find(ticket_id);
    if (!ticket) throw new SenderError(`ticket ${ticket_id} not found`);
    if (TDM14_CLOSED_TICKET.includes(ticket.status)) {
      throw new SenderError(`ticket ${ticket_id} is ${ticket.status}; not buying data`);
    }
    const seller = ctx.db.contributor.id.find(seller_id);
    if (!seller) throw new SenderError(`seller (contributor) ${seller_id} not found`);
    if (seller.kind !== 'dataset_seller') {
      throw new SenderError(`contributor ${seller_id} is kind ${seller.kind}, not dataset_seller`);
    }
    if (price_per_episode_cents <= 0n) throw new SenderError('price_per_episode_cents must be > 0');

    const c = tdm14Commitment(ctx, ticket_id, 0n);
    const needed = Math.max(0, c.target - c.committed);
    if (needed === 0) {
      throw new SenderError(
        `no episodes needed for ticket ${ticket_id}: target ${c.target}, ` +
          `already uploaded/accepted ${Math.max(c.existing, c.dataset_accepted)}, ` +
          `reserved by open offers ${c.reserved_by_offers}`
      );
    }
    const offer = ctx.db.datasetOffer.insert({
      id: 0n,
      ticket_id,
      seller_id,
      source_uri,
      price_per_episode_cents,
      needed_count: needed,
      ingested_count: 0,
      accepted_count: 0,
      status: 'proposed',
      created_at: ctx.timestamp,
      updated_at: ctx.timestamp,
    });
    tdm14Log(ctx, role, 'dataset_offer.created', {
      offer_id: offer.id,
      ticket_id,
      seller_id,
      price_per_episode_cents,
      needed_count: needed,
      max_cost_cents: BigInt(needed) * price_per_episode_cents,
      math: c,
    });
  }
);

/** The seller (or a service on their behalf) accepts a proposed offer. */
export const accept_dataset_offer = spacetimedb.reducer(
  { offer_id: t.u64() },
  (ctx, { offer_id }) => {
    const offer = tdm14Offer(ctx, offer_id);
    const actor = tdm14RequireServiceOrSeller(ctx, offer.seller_id, ['orchestrator', 'mcp', 'web']);
    if (offer.status !== 'proposed') {
      throw new SenderError(`dataset_offer ${offer_id} is ${offer.status}, not proposed`);
    }
    ctx.db.datasetOffer.id.update({ ...offer, status: 'accepted', updated_at: ctx.timestamp });
    tdm14Log(ctx, actor, 'dataset_offer.accepted', {
      offer_id,
      ticket_id: offer.ticket_id,
      seller_id: offer.seller_id,
      needed_count: offer.needed_count,
    });
  }
);

/**
 * One delivered episode from the seller. Creates episode(kind=purchased,
 * review_status=pending), bumps ingested_count and dataset.total_count.
 * Rejected once the offer's live deliveries reach needed_count, or once the
 * ticket as a whole already has target_episodes committed.
 */
export const ingest_purchased_episode = spacetimedb.reducer(
  {
    offer_id: t.u64(),
    storage_uri: t.string(),
    actions_uri: t.string(),
    cameras_json: t.string(),
    frame_count: t.u32(),
    duration_ms: t.u32(),
    success: t.option(t.bool()),
  },
  (ctx, { offer_id, storage_uri, actions_uri, cameras_json, frame_count, duration_ms, success }) => {
    const offer = tdm14Offer(ctx, offer_id);
    const actor = tdm14RequireServiceOrSeller(ctx, offer.seller_id, [
      'orchestrator',
      'factory',
      'web',
    ]);
    if (!TDM14_INGESTABLE.includes(offer.status)) {
      throw new SenderError(
        `dataset_offer ${offer_id} is ${offer.status}; ingest needs ${TDM14_INGESTABLE.join('|')}`
      );
    }
    if (!storage_uri) throw new SenderError('storage_uri is required');
    tdm14ParseJson('cameras_json', cameras_json);
    const ticket = ctx.db.ticket.id.find(offer.ticket_id);
    if (!ticket) throw new SenderError(`ticket ${offer.ticket_id} not found`);

    const live = tdm14LiveCount(ctx, tdm14OfferEpisodes(ctx, offer.ticket_id, offer_id));
    if (live >= offer.needed_count) {
      throw new SenderError(
        `dataset_offer ${offer_id} is full: ${live}/${offer.needed_count} needed episodes ` +
          `already delivered (ingested_count=${offer.ingested_count}); extra episodes are not bought`
      );
    }
    // Re-check the ticket-wide cap: other uploads may have landed since the offer was made.
    // c.existing already includes this offer's live deliveries; this one adds 1.
    const c = tdm14Commitment(ctx, offer.ticket_id, offer_id);
    if (c.committed >= c.target) {
      throw new SenderError(
        `ticket ${offer.ticket_id} already has its ${c.target} target episodes ` +
          `(uploaded/accepted ${Math.max(c.existing, c.dataset_accepted)}, reserved by other offers ` +
          `${c.reserved_by_offers}); dataset_offer ${offer_id} cannot take more`
      );
    }

    const ep = ctx.db.episode.insert({
      id: 0n,
      ticket_id: offer.ticket_id,
      robot_id: ticket.robot_id,
      task_id: 0n,
      contributor_id: offer.seller_id,
      dataset_offer_id: offer_id,
      kind: 'purchased',
      review_status: 'pending',
      checkpoint_uri: undefined,
      storage_uri,
      cameras_json,
      actions_uri,
      frame_count,
      duration_ms,
      takeover_ts: undefined,
      handback_ts: undefined,
      success,
      created_at: ctx.timestamp,
    });
    const nowLive = live + 1;
    ctx.db.datasetOffer.id.update({
      ...offer,
      ingested_count: offer.ingested_count + 1,
      status: nowLive >= offer.needed_count ? 'qa' : 'ingesting',
      updated_at: ctx.timestamp,
    });
    const ds = ctx.db.dataset.ticket_id.find(offer.ticket_id);
    if (ds) {
      ctx.db.dataset.id.update({ ...ds, total_count: ds.total_count + 1, updated_at: ctx.timestamp });
    }
    tdm14Log(ctx, actor, 'dataset_offer.episode_ingested', {
      offer_id,
      episode_id: ep.id,
      ticket_id: offer.ticket_id,
      live: nowLive,
      needed_count: offer.needed_count,
      ingested_count: offer.ingested_count + 1,
    });
  }
);

/** One deterministic (or LLM) QA check on an episode. Latest row per check_name wins. */
export const record_qa_result = spacetimedb.reducer(
  { episode_id: t.u64(), check_name: t.string(), passed: t.bool(), detail_json: t.string() },
  (ctx, { episode_id, check_name, passed, detail_json }) => {
    const role = requireRole(ctx, 'reviewer', 'orchestrator');
    const ep = ctx.db.episode.id.find(episode_id);
    if (!ep) throw new SenderError(`episode ${episode_id} not found`);
    if (!check_name.trim()) throw new SenderError('check_name is required');
    tdm14ParseJson('detail_json', detail_json);
    const row = ctx.db.qaResult.insert({
      id: 0n,
      episode_id,
      check_name,
      passed,
      detail_json,
      created_at: ctx.timestamp,
    });
    tdm14Log(ctx, role, 'qa.result_recorded', {
      qa_result_id: row.id,
      episode_id,
      ticket_id: ep.ticket_id,
      dataset_offer_id: ep.dataset_offer_id,
      check_name,
      passed,
    });
  }
);

/**
 * Settle an offer once every delivered episode has a verdict.
 * accepted_count = min(needed_count, episodes with passing QA checks AND an
 * accepted review). Status -> done (the "settled" state). Payout =
 * accepted_count x price_per_episode_cents, logged for the payments step.
 */
export const finalize_offer = spacetimedb.reducer(
  { offer_id: t.u64() },
  (ctx, { offer_id }) => {
    const role = requireRole(ctx, 'orchestrator', 'reviewer', 'payments');
    const offer = tdm14Offer(ctx, offer_id);
    if (!TDM14_INGESTABLE.includes(offer.status)) {
      throw new SenderError(`dataset_offer ${offer_id} is ${offer.status}; cannot finalize`);
    }
    const eps = tdm14OfferEpisodes(ctx, offer.ticket_id, offer_id);
    const pending = eps.filter((e) => tdm14Verdict(ctx, e) === 'pending').map((e) => e.id);
    if (pending.length > 0) {
      throw new SenderError(
        `dataset_offer ${offer_id} has ${pending.length} episode(s) without a QA verdict: ` +
          pending.join(',')
      );
    }
    const billableIds = eps.filter((e) => tdm14Billable(ctx, e)).map((e) => e.id);
    const accepted = Math.min(offer.needed_count, billableIds.length);
    ctx.db.datasetOffer.id.update({
      ...offer,
      accepted_count: accepted,
      status: 'done',
      updated_at: ctx.timestamp,
    });
    tdm14Log(ctx, role, 'dataset_offer.settled', {
      offer_id,
      ticket_id: offer.ticket_id,
      seller_id: offer.seller_id,
      needed_count: offer.needed_count,
      ingested_count: offer.ingested_count,
      accepted_count: accepted,
      billable_episode_ids: billableIds.slice(0, accepted),
      payout_cents: BigInt(accepted) * offer.price_per_episode_cents,
    });
  }
);

/** Withdraw an open offer; its reserved slots go back to the ticket. */
export const cancel_dataset_offer = spacetimedb.reducer(
  { offer_id: t.u64(), reason: t.string() },
  (ctx, { offer_id, reason }) => {
    const role = requireRole(ctx, 'orchestrator', 'mcp', 'web');
    const offer = tdm14Offer(ctx, offer_id);
    if (!TDM14_OPEN_OFFER.includes(offer.status)) {
      throw new SenderError(`dataset_offer ${offer_id} is ${offer.status}; cannot cancel`);
    }
    ctx.db.datasetOffer.id.update({ ...offer, status: 'cancelled', updated_at: ctx.timestamp });
    tdm14Log(ctx, role, 'dataset_offer.cancelled', { offer_id, ticket_id: offer.ticket_id, reason });
  }
);
