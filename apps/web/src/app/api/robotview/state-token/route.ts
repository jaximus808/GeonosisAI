// TDM-96: a VIEW-scope stream token for the robot's live state (/ws/state) and cameras, never /control.
// Proxies POST /stream/token {scope:"view"} on the orchestrator (its CORS is GET-only). Two ways in:
//   {robot_id}                              operator login (rdf_op cookie, requireOperator()); we add
//                                           X-RDF-Operator-Key (RDF_OPERATOR_KEY) on the server only
//   {task_id, identity, spacetime_token}    the live claimant of that task (checked by the orchestrator
//                                           against SpacetimeDB, like the TDM-55 camera token)
// The browser's SpacetimeDB token is passed through and never logged.

import type { NextRequest } from 'next/server';
import { requireOperator } from '@/lib/operator/session';
import { tdm78Env, tdm78Orchestrator } from '@/lib/tdm78/server';

const ID = /^\d{1,15}$/;

export async function POST(req: NextRequest) {
  const b = (await req.json().catch(() => ({}))) as {
    robot_id?: unknown;
    task_id?: unknown;
    identity?: unknown;
    spacetime_token?: unknown;
  };
  let body: Record<string, unknown>;
  const headers: Record<string, string> = { 'content-type': 'application/json' };
  if (typeof b.task_id === 'number' && typeof b.identity === 'string' && typeof b.spacetime_token === 'string') {
    body = { scope: 'view', task_id: b.task_id, identity: b.identity, spacetime_token: b.spacetime_token };
  } else if (typeof b.robot_id === 'number' && ID.test(String(b.robot_id))) {
    const denied = requireOperator(req);
    if (denied) return denied;
    const key = tdm78Env('RDF_OPERATOR_KEY');
    if (!key) return Response.json({ detail: 'RDF_OPERATOR_KEY is not set on the web server' }, { status: 503 });
    headers['x-rdf-operator-key'] = key;
    body = { scope: 'view', robot_id: b.robot_id };
  } else {
    return Response.json({ detail: 'send {robot_id} (operator) or {task_id, identity, spacetime_token} (claimant)' }, { status: 422 });
  }
  try {
    const res = await fetch(`${tdm78Orchestrator()}/stream/token`, {
      method: 'POST',
      headers,
      body: JSON.stringify(body),
      cache: 'no-store',
      signal: AbortSignal.timeout(15_000),
    });
    return new Response(await res.text(), {
      status: res.status,
      headers: { 'content-type': res.headers.get('content-type') ?? 'application/json', 'cache-control': 'no-store' },
    });
  } catch (e) {
    return Response.json({ detail: `Orchestrator not reachable at ${tdm78Orchestrator()} (${(e as Error).message})` }, { status: 502 });
  }
}
