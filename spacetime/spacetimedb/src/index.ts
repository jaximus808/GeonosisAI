// Robot Deployment Factory: SpacetimeDB module entry.
// Tables: schema.ts (TDM-8). Allowed enum values + defaults: enums.ts.
// ctx.sender helpers: auth.ts. Reducer tickets (TDM-9..15) add their reducers
// in their own files and re-export them here with `export * from './<file>'`.
// Only export values registered with SpacetimeDB from this file.
import { t, SenderError } from 'spacetimedb/server';
import { Identity } from 'spacetimedb';
import spacetimedb from './schema';
import { requireOwner, requireService } from './auth';
import { logEvent, checkEventActor, checkEventType, eventActorKind } from './event_log'; // TDM-15
import { SERVICE_ROLES, oneOf } from './enums';

export default spacetimedb;

// Runs once, on first publish (or after a --delete-data publish): the
// publisher is the database owner, so seed it into the allow-list.
export const init = spacetimedb.init((ctx) => {
  ctx.db.serviceIdentity.insert({
    identity: ctx.sender,
    role: 'owner',
    label: 'database owner (CLI / SPACETIME_TOKEN)',
    added_at: ctx.timestamp,
  });
  logEvent(ctx, 'spacetime', 'infra.module_init', { schema: 'TDM-8', owner: ctx.sender.toHexString() });
});

// The maincloud DB predates this module version, so `init` never ran there.
// bootstrap_owner seeds the owner row once, and only for the known owner
// identity (the CLI login that owns robo-factory-a706j). No-op if an owner
// row already exists. Change OWNER_IDENTITY_HEX if the DB moves accounts.
const OWNER_IDENTITY_HEX = 'c2007095442e47104a80f3efaacb77b7d62cbee6050a6e5d201c076509a800e0';
export const bootstrap_owner = spacetimedb.reducer((ctx) => {
  if (!ctx.sender.equals(Identity.fromString(OWNER_IDENTITY_HEX))) {
    throw new SenderError('unauthorized: not the database owner');
  }
  if ([...ctx.db.serviceIdentity.iter()].some((r) => r.role === 'owner')) return;
  ctx.db.serviceIdentity.insert({
    identity: ctx.sender,
    role: 'owner',
    label: 'database owner (CLI / SPACETIME_TOKEN)',
    added_at: ctx.timestamp,
  });
  logEvent(ctx, 'owner', 'infra.owner_bootstrapped', { schema: 'TDM-8', owner: OWNER_IDENTITY_HEX });
});

// Append-only (TDM-15). ts is the server timestamp of the reducer call.
// Service identities only (anonymous callers are refused). actor follows the
// convention in event_log.ts; a non-owner may not log as another service role
// or as 'spacetime' (the module itself). payload_json must be valid JSON
// ('' is stored as '{}') and is stored verbatim.
export const append_event = spacetimedb.reducer(
  { actor: t.string(), type: t.string(), payload_json: t.string() },
  (ctx, { actor, type, payload_json }) => {
    const role = requireService(ctx);
    checkEventActor(actor);
    checkEventType(type);
    const kind = eventActorKind(actor);
    if (role !== 'owner' && kind !== role && ((SERVICE_ROLES as readonly string[]).includes(kind) || kind === 'spacetime')) {
      throw new SenderError(`a ${role} identity may not log as actor ${JSON.stringify(actor)}`);
    }
    const body = payload_json.trim() === '' ? '{}' : payload_json;
    if (body.length > 64_000) throw new SenderError('payload_json is over 64 KB');
    try {
      JSON.parse(body);
    } catch {
      throw new SenderError('payload_json must be valid JSON');
    }
    logEvent(ctx, actor, type, body);
  }
);

// Owner-only: allow-list a per-service identity (from POST /v1/identity).
export const register_service_identity = spacetimedb.reducer(
  { identity: t.identity(), role: t.string(), label: t.string() },
  (ctx, { identity, role, label }) => {
    requireOwner(ctx);
    oneOf('role', role, SERVICE_ROLES);
    if (role === 'owner') throw new SenderError('cannot grant owner');
    const row = { identity, role, label, added_at: ctx.timestamp };
    if (ctx.db.serviceIdentity.identity.find(identity)) {
      ctx.db.serviceIdentity.identity.update(row);
    } else {
      ctx.db.serviceIdentity.insert(row);
    }
    logEvent(ctx, 'owner', 'infra.service_identity_registered', { identity: identity.toHexString(), role, label });
  }
);

// Owner-only: remove a service identity from the allow-list.
export const revoke_service_identity = spacetimedb.reducer(
  { identity: t.identity() },
  (ctx, { identity }) => {
    requireOwner(ctx);
    const row = ctx.db.serviceIdentity.identity.find(identity);
    if (!row) throw new SenderError('unknown service identity');
    if (row.role === 'owner') throw new SenderError('cannot revoke owner');
    ctx.db.serviceIdentity.identity.delete(identity);
    logEvent(ctx, 'owner', 'infra.service_identity_revoked', { identity: identity.toHexString(), role: row.role });
  }
);
export * from './reducers/tickets'; // TDM-9
export * from './reducers/training_eval'; // TDM-12
export * from './reducers/agent_robot'; // TDM-13
export * from './reducers/dataset_offer'; // TDM-14
export * from './reducers/marketplace'; // TDM-10
export * from './reducers/episodes_payments'; // TDM-11
export * from './reducers/contributors'; // TDM-60
