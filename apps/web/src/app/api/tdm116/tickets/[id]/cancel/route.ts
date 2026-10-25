// TDM-116: "Cancel ticket" from the ticket cockpit (top-bar menu and the Settings tab).
//
//   POST /api/tdm116/tickets/{id}/cancel  {reason?}  -> set_ticket_status(id, "cancelled", note)
//
// Operator login required (requireOperator, lib/operator/session.ts). The Next server calls the reducer
// itself with the web service identity (RDF_WEB_SPACETIME_TOKEN, role `web`, which set_ticket_status
// allows for `cancelled` only); the token never reaches the browser. The reducer refuses a ticket that is
// already shipped, failed or cancelled. TDM-114 releases the ticket's robot job on the status change.

import type { NextRequest } from 'next/server';
import { requireOperator } from '@/lib/operator/session';
import { tdm78SpacetimeTarget } from '@/lib/tdm78/server';
import { tdm116CancelNote } from '@/lib/tdm116/cockpit';

type Ctx = { params: Promise<{ id: string }> };
const TDM116_ID = /^\d{1,15}$/;

export async function POST(req: NextRequest, ctx: Ctx) {
  const denied = requireOperator(req);
  if (denied) return denied;
  const headers = { 'cache-control': 'no-store' };
  const { id } = await ctx.params;
  if (!TDM116_ID.test(id)) return Response.json({ ok: false, detail: 'bad ticket id' }, { status: 400, headers });
  const b = (await req.json().catch(() => ({}))) as { reason?: unknown };
  const note = tdm116CancelNote(typeof b.reason === 'string' ? b.reason : '');
  const { host, db, token } = tdm78SpacetimeTarget();
  if (!token) {
    return Response.json({ ok: false, detail: 'RDF_WEB_SPACETIME_TOKEN is not set on the web server; cancel with the MCP connector' }, { status: 503, headers });
  }
  try {
    const res = await fetch(`${host}/v1/database/${encodeURIComponent(db)}/call/set_ticket_status`, {
      method: 'POST',
      headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' },
      body: JSON.stringify([Number(id), 'cancelled', note]),
      cache: 'no-store',
      signal: AbortSignal.timeout(8_000),
    });
    if (res.ok) return Response.json({ ok: true, note }, { headers });
    return Response.json({ ok: false, detail: (await res.text()).slice(0, 400) }, { status: res.status, headers });
  } catch (e) {
    return Response.json({ ok: false, detail: `SpacetimeDB not reachable (${(e as Error).message})` }, { status: 502, headers });
  }
}
