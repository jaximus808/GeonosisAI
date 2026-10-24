// Robot Deployment Factory: SpacetimeDB module (placeholder).
// Only the event log lives here for now. The full schema (robots, tickets,
// tasks, episodes, reviews, runs, payments, agent sessions) is a separate task.
import { schema, table, t } from 'spacetimedb/server';

const eventLog = table(
  { name: 'event_log', public: true },
  {
    id: t.u64().primaryKey().autoInc(),
    ts: t.timestamp(),
    actor: t.string(),
    type: t.string(),
    payload_json: t.string(),
  }
);

const spacetimedb = schema({ eventLog });
export default spacetimedb;

// Append-only. ts is the server timestamp of the reducer call.
export const append_event = spacetimedb.reducer(
  { actor: t.string(), type: t.string(), payload_json: t.string() },
  (ctx, { actor, type, payload_json }) => {
    ctx.db.eventLog.insert({ id: 0n, ts: ctx.timestamp, actor, type, payload_json });
  }
);
