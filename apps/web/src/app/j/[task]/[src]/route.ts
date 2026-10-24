// TDM-108: job-drop short link. Every social post / share card links (and its QR code points) to
// /j/{task}/{channel}?k=<invite>, which lands on the marketplace deep link /marketplace?task={task}&src={channel}.
// The marketplace remembers src and reports it with the claim (POST /api/tdm108/claim-src).
//
// Per-task invite (lib/tdm108/invite.ts): a valid `k` (from the QR / link) or `c` (the 6-character code typed
// at /join) is redeemed here for the scoped cookie rdf_task, which opens THIS task's pages and APIs only (and
// its open siblings: the same ticket's open tasks of the same type, posted together as one job). The task
// must still be open or claimed: an invite stops working once its task is completed, expired or cancelled.
// No invite: plain redirect, and the gate decides (site-wide code, operator, or /join).
//
// A short path survives the access gates' /join?next=... redirect intact. The Location is relative, so it
// stays on whatever host the phone used (Vercel, tunnel or LAN).

import { tdm108ShortTarget } from '@/lib/tdm108/proxy';
import {
  TDM108_TASK_COOKIE,
  TDM108_TASK_TTL_S,
  tdm108CheckInvite,
  tdm108InviteSecret,
  tdm108MintTaskCookie,
} from '@/lib/tdm108/invite';
import { tdm78Env, tdm78SpacetimeTarget } from '@/lib/tdm78/server';
import { tdm110SqlRows } from '@/lib/tdm110/edge';

type Ctx = { params: Promise<{ task: string; src: string }> };

const LIVE = new Set(['open', 'claimed', 'submitted']);

function redirect(to: string, cookie?: string): Response {
  const headers: Record<string, string> = { location: to, 'cache-control': 'no-store' };
  if (cookie) headers['set-cookie'] = cookie;
  return new Response(null, { status: 302, headers });
}

async function sql(query: string): Promise<Record<string, unknown>[]> {
  const { host, db } = tdm78SpacetimeTarget();
  const res = await fetch(`${host}/v1/database/${encodeURIComponent(db)}/sql`, {
    method: 'POST',
    headers: { 'content-type': 'text/plain' },
    body: query,
    cache: 'no-store',
    signal: AbortSignal.timeout(6_000),
  });
  if (!res.ok) throw new Error(`SpacetimeDB SQL ${res.status}`);
  return tdm110SqlRows(await res.json());
}

function gone(task: string): Response {
  const html = `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Task ${task} is no longer available</title>
<body style="font-family:system-ui,sans-serif;background:#13110f;color:#f1e9e1;padding:24px;max-width:32rem;margin:auto">
<h1 style="font-size:22px">Task #${task} is no longer available.</h1>
<p style="color:#b3a59a">Someone finished it, or it was withdrawn. This link only opened that one task.</p>
<p><a style="color:#79c7ff" href="/">GeonosisAI</a></p></body>`;
  return new Response(html, { status: 410, headers: { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' } });
}

export async function GET(req: Request, ctx: Ctx) {
  const { task, src } = await ctx.params;
  const target = tdm108ShortTarget(task, src);
  if (!target) return redirect('/marketplace');
  const q = new URL(req.url).searchParams;
  const k = q.get('k');
  const c = q.get('c');
  if (!k && !c) return redirect(target);
  const secret = tdm108InviteSecret({ RDF_DROP_INVITE_SECRET: tdm78Env('RDF_DROP_INVITE_SECRET'), RDF_EDGE_SECRET: tdm78Env('RDF_EDGE_SECRET') });
  const id = Number(task);
  if (!tdm108CheckInvite(secret, id, src, k, c)) {
    // a wrong typed code goes back to the code form; a bad link still lands where the gate decides
    return redirect(c ? `/join?next=${encodeURIComponent(`/j/${task}/${src}`)}&bad=1` : target);
  }
  let rows: Record<string, unknown>[];
  try {
    rows = await sql(`SELECT * FROM task WHERE id = ${id}`);
  } catch {
    return new Response('The factory database is not reachable right now. Try again in a minute.', { status: 503 });
  }
  const t = rows[0];
  if (!t || !LIVE.has(String(t.status))) return gone(task);
  const ticket = Number(t.ticket_id);
  const robot = Number(t.robot_id);
  let ids = [id];
  try {
    const sib = await sql(`SELECT * FROM task WHERE ticket_id = ${ticket}`);
    ids = [id, ...sib
      .filter((r) => Number(r.id) !== id && r.type === t.type && r.status === 'open')
      .map((r) => Number(r.id))
      .slice(0, 15)];
  } catch {
    // siblings are a convenience; the task itself is enough
  }
  const https = (req.headers.get('x-forwarded-proto') || new URL(req.url).protocol.replace(':', '')).split(',')[0].trim() === 'https';
  const cookie = [
    `${TDM108_TASK_COOKIE}=${tdm108MintTaskCookie(secret, ids, ticket, robot)}`,
    'Path=/',
    `Max-Age=${TDM108_TASK_TTL_S}`,
    'HttpOnly',
    'SameSite=Lax',
    ...(https ? ['Secure'] : []),
  ].join('; ');
  return redirect(target, cookie);
}
