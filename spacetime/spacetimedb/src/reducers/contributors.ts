// TDM-60: contributor identity and presence.
//
// Identity: a contributor IS a SpacetimeDB connection identity. The browser
// connects anonymously the first time, the server issues it a token, and the
// web app keeps that token in localStorage, so a refresh reconnects as the same
// identity (apps/web/src/lib/spacetime/provider.tsx). register_contributor
// binds ctx.sender to one contributor row; no passwords, no identity args.
// Service identities (the owner token included) are refused, so the owner
// token can never become a contributor by accident.
//
// Presence: marketplace pages call contributor_heartbeat every 30 s. The first
// heartbeat flips contributor.online to true and logs contributor.online; later
// ones only move contributor_presence.last_seen, so steady heartbeats don't
// rewrite the contributor row or flood the event log. A page that is left calls
// contributor_offline. A scheduled sweep (every 30 s) marks anyone whose
// last_seen is older than PRESENCE_STALE_MICROS offline, so a dead phone or a
// closed laptop drops off within ~105 s even if it never said goodbye.
//
// Email (optional) goes to the private contributor_contact table, never to a
// public table or the event log. Stripe fields (stripe_account_id,
// payouts_enabled) are left to the payments ticket (TDM-63).
import { t, SenderError } from 'spacetimedb/server';
import { ScheduleAt } from 'spacetimedb';
import spacetimedb, { contributorPresenceSweep, type RdfCtx } from '../schema';
import { serviceRole } from '../auth';
import { CONTRIBUTOR_KINDS, oneOf } from '../enums';
import { logEvent } from '../event_log'; // TDM-15: the only event_log write path

const PRESENCE_STALE_MICROS = 75n * 1_000_000n; // 2.5 missed 30 s heartbeats
const SWEEP_INTERVAL_MICROS = 30n * 1_000_000n;
const MAX_NAME = 64;
const MAX_EMAIL = 254;
const MAX_DEVICES = 8;
const DEVICE_RE = /^[a-z0-9_]{1,32}$/;
const PAGE_RE = /^[a-z0-9_\-/]{1,64}$/;
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

type ContributorRow = NonNullable<ReturnType<RdfCtx['db']['contributor']['id']['find']>>;

// ---------------------------------------------------------------- helpers
// Not exported (index.ts re-exports this file with `export *`); tdm60 prefix
// keeps helper names unique in the shared module.

function tdm60Log(ctx: RdfCtx, actor: string, type: string, payload: Record<string, unknown>) {
  logEvent(ctx, actor, type, payload);
}

/** The contributor bound to ctx.sender, if any. */
function tdm60Me(ctx: RdfCtx): ContributorRow | undefined {
  for (const c of ctx.db.contributor.iter()) {
    if (c.identity != null && c.identity.equals(ctx.sender)) return c;
  }
  return undefined;
}

function tdm60RequireMe(ctx: RdfCtx): ContributorRow {
  const me = tdm60Me(ctx);
  if (!me) throw new SenderError('not a contributor: call register_contributor first (open /join)');
  return me;
}

function tdm60CleanName(raw: string, ctx: RdfCtx): string {
  const name = raw.trim().replace(/\s+/g, ' ');
  if (name.length > MAX_NAME) throw new SenderError(`name longer than ${MAX_NAME} chars`);
  if (/[\u0000-\u001f\u007f]/.test(name)) throw new SenderError('name has control characters');
  // Anonymous join: a stable guest name from the identity.
  return name || `guest-${ctx.sender.toHexString().slice(0, 6)}`;
}

function tdm60CleanDevices(devices: string[]): string[] {
  if (devices.length > MAX_DEVICES) throw new SenderError(`at most ${MAX_DEVICES} devices`);
  const out: string[] = [];
  for (const d of devices) {
    if (!DEVICE_RE.test(d)) throw new SenderError(`bad device "${d}" (lowercase a-z0-9_, max 32)`);
    if (!out.includes(d)) out.push(d);
  }
  return out;
}

function tdm60CleanEmail(raw: string): string {
  const email = raw.trim().toLowerCase();
  if (email === '') return '';
  if (email.length > MAX_EMAIL || !EMAIL_RE.test(email)) throw new SenderError('invalid email');
  return email;
}

function tdm60TouchPresence(ctx: RdfCtx, contributorId: bigint, page: string) {
  const row = { contributor_id: contributorId, last_seen: ctx.timestamp, page };
  if (ctx.db.contributorPresence.contributor_id.find(contributorId)) {
    ctx.db.contributorPresence.contributor_id.update(row);
  } else {
    ctx.db.contributorPresence.insert(row);
  }
}

function tdm60EnsureSweep(ctx: RdfCtx) {
  for (const _ of ctx.db.contributorPresenceSweep.iter()) return;
  ctx.db.contributorPresenceSweep.insert({
    scheduled_id: 0n,
    scheduled_at: ScheduleAt.interval(SWEEP_INTERVAL_MICROS),
  });
}

function tdm60SetOnline(ctx: RdfCtx, c: ContributorRow, online: boolean, reason: string, page = '') {
  if (c.online === online) return;
  ctx.db.contributor.id.update({ ...c, online });
  const actor = reason === 'stale' ? 'spacetime' : `contributor:${c.id}`;
  tdm60Log(ctx, actor, online ? 'contributor.online' : 'contributor.offline', {
    contributor_id: c.id,
    name: c.name,
    reason,
    ...(page ? { page } : {}),
  });
}

// ---------------------------------------------------------------- reducers

/**
 * Sign up (or update) the caller as a contributor. Idempotent per identity:
 * calling again updates name/devices/email. `name` "" gives a guest name,
 * `email` "" leaves the stored email unchanged. `kind` is fixed at creation.
 * `source_ticket_id` is the ticket whose QR brought them here (0 = none),
 * recorded in the event log only.
 */
export const register_contributor = spacetimedb.reducer(
  {
    name: t.string(),
    email: t.string(),
    devices: t.array(t.string()),
    kind: t.string(),
    source_ticket_id: t.u64(),
  },
  (ctx, { name, email, devices, kind, source_ticket_id }) => {
    const role = serviceRole(ctx);
    if (role !== undefined) {
      throw new SenderError(`service identity (${role}) cannot be a contributor; use a browser identity`);
    }
    oneOf('kind', kind, CONTRIBUTOR_KINDS);
    const cleanName = tdm60CleanName(name, ctx);
    const cleanDevices = tdm60CleanDevices(devices);
    const cleanEmail = tdm60CleanEmail(email);

    const existing = tdm60Me(ctx);
    let row: ContributorRow;
    if (existing) {
      if (existing.kind !== kind) {
        throw new SenderError(`kind is fixed at sign-up (${existing.kind})`);
      }
      row = { ...existing, name: cleanName, devices: cleanDevices, online: true };
      ctx.db.contributor.id.update(row);
      tdm60Log(ctx, `contributor:${row.id}`, 'contributor.updated', {
        contributor_id: row.id,
        name: row.name,
        devices: row.devices,
        has_email: cleanEmail !== '' || ctx.db.contributorContact.contributor_id.find(row.id) != null,
        source_ticket_id,
      });
    } else {
      row = ctx.db.contributor.insert({
        id: 0n,
        identity: ctx.sender,
        kind,
        name: cleanName,
        devices: cleanDevices,
        online: true,
        stripe_account_id: undefined,
        payouts_enabled: false,
        total_paid_cents: 0n,
        created_at: ctx.timestamp,
      });
      tdm60Log(ctx, `contributor:${row.id}`, 'contributor.registered', {
        contributor_id: row.id,
        identity: ctx.sender.toHexString(),
        kind,
        name: row.name,
        devices: row.devices,
        has_email: cleanEmail !== '',
        source_ticket_id,
      });
    }
    if (cleanEmail !== '') {
      const contact = { contributor_id: row.id, email: cleanEmail, updated_at: ctx.timestamp };
      if (ctx.db.contributorContact.contributor_id.find(row.id)) {
        ctx.db.contributorContact.contributor_id.update(contact);
      } else {
        ctx.db.contributorContact.insert(contact);
      }
    }
    tdm60TouchPresence(ctx, row.id, 'join');
    tdm60EnsureSweep(ctx);
  }
);

/**
 * Presence heartbeat from an open marketplace page (every 30 s). Marks the
 * caller online (logging only the transition) and moves last_seen.
 */
export const contributor_heartbeat = spacetimedb.reducer({ page: t.string() }, (ctx, { page }) => {
  if (!PAGE_RE.test(page)) throw new SenderError('bad page (a-z0-9_-/, max 64)');
  const me = tdm60RequireMe(ctx);
  tdm60TouchPresence(ctx, me.id, page);
  tdm60SetOnline(ctx, me, true, 'heartbeat', page);
  tdm60EnsureSweep(ctx);
});

/** The caller left every marketplace page (tab closed / navigated away). */
export const contributor_offline = spacetimedb.reducer((ctx) => {
  const me = tdm60RequireMe(ctx);
  tdm60SetOnline(ctx, me, false, 'left');
});

/**
 * Scheduled every 30 s: contributors whose last heartbeat is older than 75 s
 * go offline. Safe for anyone to call: it only applies the time rule.
 */
export const sweep_contributor_presence = spacetimedb.reducer(
  { onSchedule: contributorPresenceSweep },
  { timer: contributorPresenceSweep.rowType },
  (ctx) => {
    const cutoff = ctx.timestamp.microsSinceUnixEpoch - PRESENCE_STALE_MICROS;
    for (const c of [...ctx.db.contributor.iter()]) {
      if (!c.online) continue;
      const p = ctx.db.contributorPresence.contributor_id.find(c.id);
      if (!p || p.last_seen.microsSinceUnixEpoch < cutoff) tdm60SetOnline(ctx, c, false, 'stale');
    }
  }
);
