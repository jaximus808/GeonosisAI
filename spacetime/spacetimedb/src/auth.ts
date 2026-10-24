// ctx.sender checks for reducers (WORKER_GUIDE security note).
// Anonymous callers can invoke any reducer, so every reducer that changes
// tickets, tasks, reviews, payments or robot commands must call one of these.
// The allow-list is the private service_identity table; `init` seeds the owner
// (the CLI/SPACETIME_TOKEN identity), and the owner adds per-service
// identities with the register_service_identity reducer.
import { SenderError } from 'spacetimedb/server';
import type { RdfCtx } from './schema';

/** Returns the caller's role, or undefined if not on the allow-list. */
export function serviceRole(ctx: RdfCtx): string | undefined {
  return ctx.db.serviceIdentity.identity.find(ctx.sender)?.role;
}

/** Any allow-listed service identity (owner included). */
export function requireService(ctx: RdfCtx): string {
  const role = serviceRole(ctx);
  if (role === undefined) throw new SenderError('unauthorized: not a service identity');
  return role;
}

/** One of the given roles; the owner always passes. */
export function requireRole(ctx: RdfCtx, ...roles: string[]): string {
  const role = requireService(ctx);
  if (role !== 'owner' && !roles.includes(role)) {
    throw new SenderError(`unauthorized: role ${role} not in ${roles.join('|')}`);
  }
  return role;
}

export function requireOwner(ctx: RdfCtx): void {
  requireRole(ctx);
}
