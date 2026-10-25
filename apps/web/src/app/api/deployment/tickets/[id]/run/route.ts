// [SIM] "Run my policy" on a shipped ticket's deployment page.
//
//   POST /api/deployment/tickets/{id}/run  -> POST /tickets/{id}/deployment/run on the orchestrator
//
// Operator only: requireOperator() first, then this server adds X-RDF-Operator-Key (RDF_OPERATOR_KEY), which
// never reaches the browser. The orchestrator refuses real and teleop-only robots and answers 409 robot_busy.
//
// TDM-126 "Real arm": body {real_arm: true, confirm: "HANDS_ON_ESTOP", checkpoint: "before"|"after", robot_id?}.
// Only those four fields are forwarded (never a checkpoint URI: the orchestrator resolves it), and every real-arm
// gate is the orchestrator's (confirm, robot advertises policy, shipped real checkpoint).

import type { NextRequest } from 'next/server';
import { requireOperator } from '@/lib/operator/session';
import { tdm78Env, tdm78Orchestrator } from '@/lib/tdm78/server';

type Ctx = { params: Promise<{ id: string }> };
const HUMAN_ID = /^[A-Za-z0-9._@:+-]{1,64}$/;

export async function POST(req: NextRequest, ctx: Ctx) {
  const denied = requireOperator(req);
  if (denied) return denied;
  const { id } = await ctx.params;
  if (!/^\d{1,19}$/.test(id)) return Response.json({ detail: 'bad ticket id' }, { status: 404 });
  const key = tdm78Env('RDF_OPERATOR_KEY');
  if (!key) return Response.json({ detail: { code: 'no_operator_key', message: 'RDF_OPERATOR_KEY is not set on the web server' } }, { status: 503 });
  const headers: Record<string, string> = { 'content-type': 'application/json', 'x-rdf-operator-key': key };
  const raw = (await req.json().catch(() => ({}))) as Record<string, unknown> | null;
  const body: Record<string, unknown> = {};
  if (raw && raw.real_arm === true) {
    body.real_arm = true;
    if (typeof raw.confirm === 'string') body.confirm = raw.confirm.slice(0, 64);
    if (raw.checkpoint === 'before' || raw.checkpoint === 'after') body.checkpoint = raw.checkpoint;
    if (typeof raw.robot_id === 'number' && Number.isInteger(raw.robot_id) && raw.robot_id > 0) body.robot_id = raw.robot_id;
  }
  const who = req.headers.get('x-human-id');
  if (who && HUMAN_ID.test(who)) headers['x-human-id'] = who;
  try {
    const res = await fetch(`${tdm78Orchestrator()}/tickets/${id}/deployment/run`, {
      method: 'POST',
      headers,
      body: JSON.stringify(body),
      cache: 'no-store',
      signal: AbortSignal.timeout(20_000),
    });
    return new Response(await res.text(), {
      status: res.status,
      headers: { 'content-type': res.headers.get('content-type') ?? 'application/json', 'cache-control': 'no-store' },
    });
  } catch (e) {
    return Response.json(
      { detail: { code: 'orchestrator_unreachable', message: `Orchestrator not reachable at ${tdm78Orchestrator()} (${(e as Error).message})` } },
      { status: 502 },
    );
  }
}
