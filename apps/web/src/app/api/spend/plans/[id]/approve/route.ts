// "Approve plan" on the ticket page's data-plan step (TDM-68 DecisionTrail): approve a planner's data
// plan at its estimated cost. Until now only the MCP connector could call approve_spend for a plan
// (TDM-64 has no web path), so a demo run stalled at `waiting for approve_spend`.
//
// Operator login required (requireOperator, lib/operator/session.ts). The Next server calls the
// approve_spend reducer itself with the web service identity (RDF_WEB_SPACETIME_TOKEN, role `web`,
// one of the reducer's TDM11_SPEND_APPROVERS); the token never reaches the browser. approved_cents 0 =
// the plan's est_cost_cents. The reducer still refuses anything over the ticket budget.

import type { NextRequest } from 'next/server';
import { requireOperator } from '@/lib/operator/session';
import { tdm78SpacetimeTarget } from '@/lib/tdm78/server';

type Ctx = { params: Promise<{ id: string }> };
const ID = /^\d{1,15}$/;

export async function POST(req: NextRequest, ctx: Ctx) {
  const denied = requireOperator(req);
  if (denied) return denied;
  const headers = { 'cache-control': 'no-store' };
  const { id } = await ctx.params;
  const b = (await req.json().catch(() => ({}))) as { ticket_id?: unknown };
  const ticketId = String(b.ticket_id ?? '');
  if (!ID.test(id) || !ID.test(ticketId)) return Response.json({ ok: false, detail: 'bad plan or ticket id' }, { status: 400, headers });
  const { host, db, token } = tdm78SpacetimeTarget();
  if (!token) {
    return Response.json({ ok: false, detail: 'RDF_WEB_SPACETIME_TOKEN is not set on the web server; approve with the MCP approve_spend tool' },
      { status: 503, headers });
  }
  try {
    const res = await fetch(`${host}/v1/database/${encodeURIComponent(db)}/call/approve_spend`, {
      method: 'POST',
      headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' },
      body: JSON.stringify([Number(ticketId), Number(id), 0, 'human:web-operator']),
      cache: 'no-store',
      signal: AbortSignal.timeout(8_000),
    });
    if (res.ok) return Response.json({ ok: true }, { headers });
    return Response.json({ ok: false, detail: (await res.text()).slice(0, 400) }, { status: res.status, headers });
  } catch (e) {
    return Response.json({ ok: false, detail: `SpacetimeDB not reachable (${(e as Error).message})` }, { status: 502, headers });
  }
}
