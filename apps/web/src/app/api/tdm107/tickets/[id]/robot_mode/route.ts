// TDM-107: server-side proxy for a ticket's robot mode (sim robot | real YAM).
//
//   GET  /api/tdm107/tickets/{id}/robot_mode  -> GET  /tickets/{id}/robot_mode   (public: mode, robots, history)
//   POST /api/tdm107/tickets/{id}/robot_mode  -> POST same {mode, robot_id?, reason?}
//
// The switch is operator-only: requireOperator() first, then this server adds X-RDF-Operator-Key
// (RDF_OPERATOR_KEY), which never reaches the browser. X-Human-Id is passed through for the event log.

import type { NextRequest } from 'next/server';
import { requireOperator } from '@/lib/operator/session';
import { tdm78Env, tdm78Orchestrator } from '@/lib/tdm78/server';

const TDM107_TICKET = /^\d{1,19}$/;
const TDM107_HUMAN_ID = /^[A-Za-z0-9._@:+-]{1,64}$/;

type Ctx = { params: Promise<{ id: string }> };

async function tdm107Forward(path: string, init: RequestInit) {
  try {
    const res = await fetch(`${tdm78Orchestrator()}/${path}`, { ...init, cache: 'no-store', signal: AbortSignal.timeout(20_000) });
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

export async function GET(_req: NextRequest, ctx: Ctx) {
  const { id } = await ctx.params;
  if (!TDM107_TICKET.test(id)) return Response.json({ detail: 'bad ticket id' }, { status: 404 });
  return tdm107Forward(`tickets/${id}/robot_mode`, { method: 'GET' });
}

export async function POST(req: NextRequest, ctx: Ctx) {
  const denied = requireOperator(req);
  if (denied) return denied;
  const { id } = await ctx.params;
  if (!TDM107_TICKET.test(id)) return Response.json({ detail: 'bad ticket id' }, { status: 404 });
  const b = (await req.json().catch(() => null)) as { mode?: unknown; robot_id?: unknown; reason?: unknown } | null;
  if (!b || (b.mode !== 'sim' && b.mode !== 'real')) {
    return Response.json({ detail: { code: 'bad_mode', message: 'send {mode: "sim" | "real"}' } }, { status: 422 });
  }
  const body: Record<string, unknown> = { mode: b.mode };
  if (typeof b.robot_id === 'number' && Number.isInteger(b.robot_id) && b.robot_id > 0) body.robot_id = b.robot_id;
  if (typeof b.reason === 'string') body.reason = b.reason.slice(0, 500);
  const key = tdm78Env('RDF_OPERATOR_KEY');
  if (!key) return Response.json({ detail: { code: 'no_operator_key', message: 'RDF_OPERATOR_KEY is not set on the web server' } }, { status: 503 });
  const headers: Record<string, string> = { 'content-type': 'application/json', 'x-rdf-operator-key': key };
  const who = req.headers.get('x-human-id');
  if (who && TDM107_HUMAN_ID.test(who)) headers['x-human-id'] = who;
  return tdm107Forward(`tickets/${id}/robot_mode`, { method: 'POST', headers, body: JSON.stringify(body) });
}
