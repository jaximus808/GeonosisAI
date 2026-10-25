// TDM-108 per-task invites: a job-drop post grants access to ITS task only, not the whole site.
//
// The post's link is /j/<task>/<channel>?k=<invite>, and the card can print a 6-character code for the same
// (task, channel). app/j/[task]/[src]/route.ts redeems either one (if the task is still open or claimed) for
// the scoped cookie `rdf_task`, which both gates accept for that task's pages and APIs only
// (tdm108TaskScopeAllows): proxy.ts + lib/tdm110/access.ts on Vercel, scripts/demo/public_guard.py on the
// laptop edge. Everything else still needs RDF_PUBLIC_ACCESS_CODE or an operator login.
//
// Byte-for-byte the formats of services/orchestrator/src/rdf_orchestrator/job_drop_invite.py (which mints the
// invites) and public_guard.py; test vectors pinned in all three test suites:
//   secret  RDF_DROP_INVITE_SECRET, else RDF_EDGE_SECRET
//   key     HMAC-SHA256(secret, "rdf-drop-invite-v1")
//   mac     HMAC-SHA256(key, "invite|<task>|<channel>")
//   k       base64url(mac)[0:22]
//   code    CODE_ALPHABET[mac[16 + i] % 31], i = 0..5
//   cookie  "t1.<ids '-'>.<ticket>.<robot>.<exp>.<base64url(HMAC(key, 'task-access|' + body))[0:32]>", 24 h
// Pure apart from node:crypto (test/tdm108-drops.test.ts).

import { createHmac, timingSafeEqual } from 'node:crypto';

export const TDM108_TASK_COOKIE = 'rdf_task';
export const TDM108_TASK_TTL_S = 24 * 3600;
const LABEL = 'rdf-drop-invite-v1';
const CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789';

export type Tdm108Scope = { ids: number[]; ticket: number; robot: number; exp: number };

export function tdm108InviteSecret(env: Record<string, string | undefined>): string {
  return (env.RDF_DROP_INVITE_SECRET || env.RDF_EDGE_SECRET || '').trim();
}

function key(secret: string): Buffer {
  return createHmac('sha256', secret).update(LABEL).digest();
}

function mac(secret: string, task: number, channel: string): Buffer {
  return createHmac('sha256', key(secret)).update(`invite|${task}|${channel}`).digest();
}

export function tdm108InviteK(secret: string, task: number, channel: string): string {
  return mac(secret, task, channel).toString('base64url').slice(0, 22);
}

export function tdm108InviteCode(secret: string, task: number, channel: string): string {
  const m = mac(secret, task, channel);
  let out = '';
  for (let i = 0; i < 6; i++) out += CODE_ALPHABET[m[16 + i] % CODE_ALPHABET.length];
  return out;
}

function same(a: string, b: string): boolean {
  const x = Buffer.from(a);
  const y = Buffer.from(b);
  return x.length === y.length && timingSafeEqual(x, y);
}

/** A valid invite (link `k` or typed `code`) for this task + channel. */
export function tdm108CheckInvite(secret: string, task: number, channel: string, k?: string | null, code?: string | null): boolean {
  if (!secret || !Number.isSafeInteger(task) || task <= 0) return false;
  if (k && same(k, tdm108InviteK(secret, task, channel))) return true;
  if (code) return same(code.replace(/[\s\-_.]/g, '').toUpperCase(), tdm108InviteCode(secret, task, channel));
  return false;
}

export function tdm108MintTaskCookie(secret: string, ids: number[], ticket: number, robot: number,
  nowS = Math.floor(Date.now() / 1000), ttlS = TDM108_TASK_TTL_S): string {
  const body = `t1.${ids.map((i) => Math.trunc(i)).join('-')}.${Math.trunc(ticket)}.${Math.trunc(robot)}.${Math.floor(nowS + ttlS)}`;
  return `${body}.${createHmac('sha256', key(secret)).update(`task-access|${body}`).digest('base64url').slice(0, 32)}`;
}

export function tdm108VerifyTaskCookie(secret: string, value: string | undefined | null,
  nowS = Math.floor(Date.now() / 1000)): Tdm108Scope | null {
  if (!secret || !value) return null;
  const p = value.split('.');
  if (p.length !== 6 || p[0] !== 't1' || !/^\d{1,19}(-\d{1,19}){0,31}$/.test(p[1]) || !/^\d{1,19}$/.test(p[2]) ||
    !/^\d{1,19}$/.test(p[3]) || !/^\d{1,12}$/.test(p[4])) return null;
  const exp = Number(p[4]);
  if (exp <= nowS || exp > nowS + TDM108_TASK_TTL_S + 60) return null;
  const ids = p[1].split('-').map(Number);
  if (!same(value, tdm108MintTaskCookie(secret, ids, Number(p[2]), Number(p[3]), exp, 0))) return null;
  return { ids, ticket: Number(p[2]), robot: Number(p[3]), exp };
}

const num = (s: string | null | undefined) => (s && /^\d{1,19}$/.test(s) ? Number(s) : NaN);

/**
 * May a task-scoped visitor open this path? Only the task's own pages (marketplace deep link, teleop, reset,
 * practice for its ticket/robot) and the APIs those pages call (their servers still check the claim).
 * Same list as scripts/demo/public_guard.py task_scope_allows.
 */
export function tdm108TaskScopeAllows(scope: Tdm108Scope, method: string, path: string, search: string): boolean {
  const ids = new Set(scope.ids);
  const q = new URLSearchParams(search);
  let m: RegExpMatchArray | null;
  if (path === '/marketplace') return ids.has(num(q.get('task')));
  // TDM-86: the invite's own sign-up. A judge who scanned the job QR taps "Join to claim" and must get the
  // join form, not the site-wide code form (the scope still opens this task only).
  if (path === '/join') return method === 'GET' || method === 'HEAD';
  // TDM-86: the invitee's own earnings (their payout for this task); ?contributor=N (anyone's) stays gated
  if (path === '/earnings') return (method === 'GET' || method === 'HEAD') && !q.has('contributor');
  // TDM-86: the public static files those pages load: the 3D arm (practice and teleop crashed on a 401 for
  // yam.glb) and the push service worker + its icons
  if (/^\/(robot\/yam\/[\w.-]+|tdm65-sw\.js|tdm65\/[\w.-]+)$/.test(path)) return method === 'GET' || method === 'HEAD';
  if ((m = path.match(/^\/(teleop|reset)\/(\d{1,19})\/?$/))) return ids.has(Number(m[2]));
  if (path === '/practice') {
    if (num(q.get('ticket')) === scope.ticket || num(q.get('robot')) === scope.robot) return true;
    const next = q.get('next');
    if (next && next.startsWith('/') && !next.startsWith('//') && !next.startsWith('/practice')) {
      const u = new URL(next, 'http://x');
      return tdm108TaskScopeAllows(scope, 'GET', u.pathname, u.search);
    }
    return false;
  }
  if (method === 'POST' && (path === '/api/tdm55/stream-token' || path === '/api/robotview/state-token')) return true;
  if ((m = path.match(/^\/api\/tdm62\/resets\/(\d{1,19})(\/photo)?$/))) return ids.has(Number(m[1]));
  if ((m = path.match(/^\/api\/tdm57\/tasks\/(\d{1,19})\/[a-z_]+$/))) return ids.has(Number(m[1]));
  if ((m = path.match(/^\/api\/tdm104\/robots\/(\d{1,19})\/scene$/))) return Number(m[1]) === scope.robot;
  if ((m = path.match(/^\/api\/tdm108\/share\/(\d{1,19})\.png$/))) return ids.has(Number(m[1]));
  if (path === '/api/tdm108/claim-src' || path === '/api/tdm59/ping' || path.startsWith('/api/tdm65/')) return true;
  if (method === 'POST' && /^\/api\/tdm113\/(optin|state|resend|optout)$/.test(path)) return true; // TDM-113 job texts
  return false;
}
