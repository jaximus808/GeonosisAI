// The cockpit's "Done, scene reset" button: the operator finished a real robot's reset between eval trials.
//
//   POST /api/tdm116/resets/{task_id}/done  -> POST /resets/{task_id}/done on the orchestrator (reset_done.py)
//
// Operator login required (requireOperator). The orchestrator claims + completes the reset as the operator's
// contributor (RDF_RESET_TEXT_CONTRIBUTOR; else cancels it and resumes the robot), so the eval goes on.
// X-RDF-Operator-Key (RDF_OPERATOR_KEY) is added here on the server only.

import type { NextRequest } from 'next/server';
import { requireOperator } from '@/lib/operator/session';
import { tdm78Env, tdm78Orchestrator } from '@/lib/tdm78/server';

type Ctx = { params: Promise<{ id: string }> };
const ID = /^\d{1,15}$/;

export async function POST(req: NextRequest, ctx: Ctx) {
  const denied = requireOperator(req);
  if (denied) return denied;
  const headers = { 'cache-control': 'no-store' };
  const { id } = await ctx.params;
  if (!ID.test(id)) return Response.json({ detail: 'bad task id' }, { status: 400, headers });
  const key = tdm78Env('RDF_OPERATOR_KEY');
  if (!key) return Response.json({ detail: 'RDF_OPERATOR_KEY is not set on the web server' }, { status: 503, headers });
  try {
    const res = await fetch(`${tdm78Orchestrator()}/resets/${id}/done`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'x-rdf-operator-key': key },
      body: '{}',
      cache: 'no-store',
      signal: AbortSignal.timeout(15_000),
    });
    return new Response(await res.text(), {
      status: res.status,
      headers: { 'content-type': res.headers.get('content-type') ?? 'application/json', ...headers },
    });
  } catch (e) {
    return Response.json({ detail: `Orchestrator not reachable at ${tdm78Orchestrator()} (${(e as Error).message})` }, { status: 502, headers });
  }
}
