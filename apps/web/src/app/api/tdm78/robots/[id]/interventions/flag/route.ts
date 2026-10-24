// TDM-69: the robot page's "Failing" button during a collection rollout -> orchestrator
// POST /robots/{id}/interventions/flag, which pauses the running policy rollout and routes a paid
// recovery_takeover task (same path as a robot-detected failure). The operator key
// (RDF_OPERATOR_KEY) is added on the server; the browser never sees it.
//
// Operator login required (lib/operator/session.ts): 401 without a valid rdf_op session cookie.

import type { NextRequest } from 'next/server';
import { requireOperator } from '@/lib/operator/session';
import { TDM78_ROBOT_ID, tdm78FlagFailing } from '@/lib/tdm78/server';

type Ctx = { params: Promise<{ id: string }> };

export async function POST(req: NextRequest, ctx: Ctx) {
  const denied = requireOperator(req);
  if (denied) return denied;
  const { id } = await ctx.params;
  if (!TDM78_ROBOT_ID.test(id)) return Response.json({ ok: false, detail: 'bad robot id' }, { status: 400 });
  const b = (await req.json().catch(() => ({}))) as { note?: unknown };
  const note = typeof b.note === 'string' && b.note.trim() ? b.note.trim().slice(0, 300) : 'operator pressed Failing (robot page)';
  const r = await tdm78FlagFailing(Number(id), note);
  return Response.json({ ok: r.ok, elapsedMs: r.elapsedMs, ...r.body }, { status: r.status, headers: { 'cache-control': 'no-store' } });
}
