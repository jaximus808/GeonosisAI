// TDM-33: the Data Droid records the per-ticket LeRobot dataset it assembled.
//
//   set_dataset_lerobot_uri(ticket_id, lerobot_uri, episode_ids_json, summary_json)
//
// Called by the Data Droid's publish tool (factory identity) or the
// orchestrator after rdf_training.data_droid_tools assembled
// <lerobot_root>/<repo_id> with TDM-39's converter. It only points
// dataset.lerobot_uri at the new dataset; it never touches review_status,
// accepted_count or payments, so a droid cannot accept episodes through it.
// Every listed episode must belong to the ticket and already be accepted by
// review: a dataset built from anything else is refused.
//
// Logs dataset.assembled {ticket_id, dataset_id, lerobot_uri, previous_uri,
// episode_ids, accepted_count, summary}. Calling it again re-points the URI
// (a new iteration's dataset) and logs the previous one.
import { t, SenderError } from 'spacetimedb/server';
import spacetimedb from '../schema';
import { requireRole } from '../auth';
import { logEvent } from '../event_log'; // TDM-15

const TDM33_SCHEMES = ['modal-vol://', 'file://', 's3://', 'hf://', 'https://'];
const TDM33_MAX_URI = 1024;
const TDM33_MAX_SUMMARY = 16_000;
const TDM33_MAX_EPISODES = 10_000;

export const set_dataset_lerobot_uri = spacetimedb.reducer(
  { ticket_id: t.u64(), lerobot_uri: t.string(), episode_ids_json: t.string(), summary_json: t.string() },
  (ctx, { ticket_id, lerobot_uri, episode_ids_json, summary_json }) => {
    const role = requireRole(ctx, 'factory', 'orchestrator');
    const ticket = ctx.db.ticket.id.find(ticket_id);
    if (!ticket) throw new SenderError(`unknown ticket ${ticket_id}`);
    const ds = ctx.db.dataset.ticket_id.find(ticket_id);
    if (!ds) throw new SenderError(`ticket ${ticket_id} has no dataset row yet (no reviewed episodes)`);

    const uri = lerobot_uri.trim();
    if (uri === '' || uri.length > TDM33_MAX_URI) throw new SenderError('lerobot_uri must be 1..1024 chars');
    if (!TDM33_SCHEMES.some((s) => uri.startsWith(s))) {
      throw new SenderError(`lerobot_uri must start with one of ${TDM33_SCHEMES.join(' ')}`);
    }

    let parsed: unknown;
    try {
      parsed = JSON.parse(episode_ids_json);
    } catch {
      throw new SenderError('episode_ids_json is not valid JSON');
    }
    if (!Array.isArray(parsed) || parsed.length === 0 || parsed.length > TDM33_MAX_EPISODES) {
      throw new SenderError(`episode_ids_json must be a non-empty array of at most ${TDM33_MAX_EPISODES} ids`);
    }
    const ids: bigint[] = [];
    const seen = new Set<string>();
    for (const v of parsed) {
      const s = String(v);
      if (!/^[0-9]+$/.test(s)) throw new SenderError(`episode id ${JSON.stringify(v)} is not a non-negative integer`);
      if (seen.has(s)) throw new SenderError(`episode id ${s} is listed twice`);
      seen.add(s);
      const id = BigInt(s);
      const ep = ctx.db.episode.id.find(id);
      if (!ep) throw new SenderError(`episode ${s} not found`);
      if (ep.ticket_id !== ticket_id) throw new SenderError(`episode ${s} belongs to ticket ${ep.ticket_id}, not ${ticket_id}`);
      if (ep.review_status !== 'accepted') {
        throw new SenderError(`episode ${s} is ${ep.review_status}; only accepted episodes go into the dataset`);
      }
      ids.push(id);
    }

    const summaryText = summary_json.trim() === '' ? '{}' : summary_json;
    if (summaryText.length > TDM33_MAX_SUMMARY) throw new SenderError('summary_json is over 16 KB');
    let summary: unknown;
    try {
      summary = JSON.parse(summaryText);
    } catch {
      throw new SenderError('summary_json is not valid JSON');
    }

    const previous = ds.lerobot_uri;
    ctx.db.dataset.id.update({ ...ds, lerobot_uri: uri, updated_at: ctx.timestamp });
    logEvent(ctx, role, 'dataset.assembled', {
      ticket_id,
      dataset_id: ds.id,
      lerobot_uri: uri,
      previous_uri: previous,
      episode_ids: ids,
      episodes: ids.length,
      accepted_count: ds.accepted_count,
      iteration: ticket.iteration,
      summary,
    });
  }
);
