// [SIM] practice sessions on a simulated robot (orchestrator practice_api.py): start | token | stop | reset_scene.
// No task, no lease, nothing recorded; the orchestrator refuses real robots.
//
// Two ways in: a logged-in operator (requireOperator passes -> this server adds RDF_OPERATOR_KEY), or a
// contributor who proves their SpacetimeDB identity in the body ({identity, spacetime_token}, forwarded
// as-is, like the TDM-55 stream token). The operator key never reaches the browser.

import type { NextRequest } from 'next/server';
import { operatorSessionOf } from '@/lib/operator/session';
import { tdm78Env, tdm78Orchestrator } from '@/lib/tdm78/server';

type Ctx = { params: Promise<{ id: string; action: string }> };
const PATHS: Record<string, string> = {
  start: 'practice/start',
  token: 'practice/token',
  stop: 'practice/stop',
  reset_scene: 'sim/reset_scene',
};

export async function POST(req: NextRequest, ctx: Ctx) {
  const headers = { 'cache-control': 'no-store' };
  const { id, action } = await ctx.params;
  const path = PATHS[action];
  if (!/^\d{1,15}$/.test(id) || !path) return Response.json({ detail: 'bad robot id or action' }, { status: 400, headers });
  const body = (await req.json().catch(() => ({}))) as { identity?: unknown; spacetime_token?: unknown };
  const fwd: Record<string, string> = { 'content-type': 'application/json' };
  const key = tdm78Env('RDF_OPERATOR_KEY');
  if (operatorSessionOf(req) != null && key) fwd['x-rdf-operator-key'] = key;
  try {
    const res = await fetch(`${tdm78Orchestrator()}/robots/${id}/${path}`, {
      method: 'POST',
      headers: fwd,
      body: JSON.stringify({ identity: body.identity ?? null, spacetime_token: body.spacetime_token ?? null }),
      cache: 'no-store',
      signal: AbortSignal.timeout(15_000),
    });
    return Response.json(await res.json().catch(() => ({})), { status: res.status, headers });
  } catch (e) {
    return Response.json({ detail: `Orchestrator not reachable (${(e as Error).message})` }, { status: 502, headers });
  }
}
