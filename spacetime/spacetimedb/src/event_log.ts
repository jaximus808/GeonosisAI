// TDM-15: the single write path into `event_log`.
//
// event_log is the evidence for "what ran on its own overnight" (CONTEXT.md),
// so it is APPEND-ONLY:
//   - Every reducer that changes state logs through logEvent() (directly or via
//     its ticket's thin tdmNLog wrapper, which only pre-serializes the payload).
//   - Nothing in the module may call ctx.db.eventLog.insert outside this file,
//     and nothing may call ctx.db.eventLog.id.update / .delete anywhere.
//   `python3 scripts/verify_event_log.py static` enforces both rules over the
//   module source; `python3 scripts/verify_event_log.py` (live) prints the row
//   count and a chain hash so the demo can show the log is untouched.
//
// Actor convention (who caused the event):
//   orchestrator | planner | reviewer | dispatcher | droid:<role> |
//   robot:<robot_id> | human:<contributor_id> | human:via_mcp | human:web
// plus the service roles reducers already log as (owner, payments, factory,
// mcp, web, robot_client), `contributor:<id>[:task:<id>]` for a contributor
// acting as themselves, `seller` for a dataset seller, and `spacetime` for the
// module's own scheduled/init work.
//
// Not a reducer: index.ts re-exports reducers only.
import { SenderError } from 'spacetimedb/server';
import type { RdfCtx } from './schema';

// Shape checks for append_event (external input) only: the convention above is
// documented, not enforced, so live callers that predate it
// (orchestrator.diagnose, fake-robot:<id>, tdm7-spike, ...) keep working.
// kind[:qualifier...], kind lowercase, e.g. droid:training, robot:1, human:via_mcp.
const TDM15_ACTOR_RE = /^[a-z][a-z0-9_.-]*(:[^\s:]+)*$/;
// area.event, lowercase, e.g. ticket.created, planner.data_type_decided, test.smoke.
const TDM15_TYPE_RE = /^[a-z][a-z0-9_-]*(\.[a-z0-9_-]+)*$/;

/** The part of an actor before the first ':' (e.g. 'droid' for 'droid:training'). */
export function eventActorKind(actor: string): string {
  const i = actor.indexOf(':');
  return i < 0 ? actor : actor.slice(0, i);
}

/** Throws SenderError unless actor is a well-formed kind[:qualifier...] string. */
export function checkEventActor(actor: string): void {
  if (actor.length > 128 || !TDM15_ACTOR_RE.test(actor)) {
    throw new SenderError(
      `event_log actor ${JSON.stringify(actor)} must be <kind>[:<id>...] (lowercase kind, no spaces, <= 128 chars), ` +
        'e.g. orchestrator, planner, droid:training, robot:1, human:via_mcp'
    );
  }
}

/** Throws SenderError unless type is a lowercase dotted name (area.event). */
export function checkEventType(type: string): void {
  if (type.length > 128 || !TDM15_TYPE_RE.test(type)) {
    throw new SenderError(`event_log type ${JSON.stringify(type)} must look like area.event (lowercase, <= 128 chars)`);
  }
}

function tdm15Json(payload: unknown): string {
  return JSON.stringify(payload, (_k, v) =>
    typeof v === 'bigint' ? (v <= BigInt(Number.MAX_SAFE_INTEGER) ? Number(v) : v.toString()) : v
  );
}

/**
 * Append one event_log row; ts is the reducer call's server timestamp.
 * payload: an already-serialized JSON string (kept verbatim), or any value
 * (JSON-encoded; bigints become numbers when safe, else strings).
 * This is the ONLY place in the module that writes event_log.
 */
export function logEvent(ctx: RdfCtx, actor: string, type: string, payload: unknown): void {
  // Internal callers are trusted to pick actor/type; only refuse an empty one.
  // (append_event, the external entry point, also runs the shape checks.)
  if (actor === '' || type === '') throw new SenderError('event_log actor and type must be non-empty');
  const payload_json = typeof payload === 'string' ? payload : tdm15Json(payload ?? {});
  ctx.db.eventLog.insert({ id: 0n, ts: ctx.timestamp, actor, type, payload_json });
}
