// TDM-78: operator commands from the robot page (Home, Stop, Run baseline, Load checkpoint,
// Start/Stop teleop, and the TDM-51 eval trial verdict trial_outcome) -> orchestrator
// POST /robots/{id}/commands, which issues them under the orchestrator's identity. The operator key (RDF_OPERATOR_KEY) is added on the server; the
// browser never sees it. soft_estop has its own, more direct route (../estop).
//
// Operator login required (lib/operator/session.ts): 401 without a valid rdf_op session cookie.
// The soft e-stop route (../estop) is never guarded.

import type { NextRequest } from 'next/server';
import { requireOperator } from '@/lib/operator/session';
import { TDM78_OPERATOR_TYPES, TDM78_ROBOT_ID, tdm78ViaOrchestrator } from '@/lib/tdm78/server';

type Ctx = { params: Promise<{ id: string }> };

export async function POST(req: NextRequest, ctx: Ctx) {
  const denied = requireOperator(req);
  if (denied) return denied;
  const { id } = await ctx.params;
  if (!TDM78_ROBOT_ID.test(id)) return Response.json({ ok: false, detail: 'bad robot id' }, { status: 400 });
  const b = (await req.json().catch(() => null)) as { type?: unknown; payload?: unknown; ticket_id?: unknown } | null;
  if (!b || typeof b.type !== 'string' || !TDM78_OPERATOR_TYPES.has(b.type)) {
    return Response.json({ ok: false, detail: `type must be one of ${[...TDM78_OPERATOR_TYPES].join(', ')}` }, { status: 400 });
  }
  const payload = b.payload && typeof b.payload === 'object' && !Array.isArray(b.payload) ? (b.payload as Record<string, unknown>) : {};
  if (JSON.stringify(payload).length > 4096) return Response.json({ ok: false, detail: 'payload too large' }, { status: 413 });
  if (b.type === 'load_checkpoint' && typeof payload.checkpoint_uri !== 'string') {
    return Response.json({ ok: false, detail: 'load_checkpoint needs payload.checkpoint_uri' }, { status: 400 });
  }
  if (b.type === 'trial_outcome') {
    // TDM-51 verdict: the robot refuses a late / duplicate / wrong-trial one, so name the trial.
    const okInt = (v: unknown) => typeof v === 'number' && Number.isSafeInteger(v) && v >= 0;
    if (typeof payload.success !== 'boolean' || !okInt(payload.eval_run_id) || !okInt(payload.index)) {
      return Response.json({ ok: false, detail: 'trial_outcome needs payload {success: boolean, eval_run_id, index}' }, { status: 400 });
    }
    if (payload.note != null && (typeof payload.note !== 'string' || payload.note.length > 500)) {
      return Response.json({ ok: false, detail: 'note must be a string of at most 500 characters' }, { status: 400 });
    }
  }
  const ticketId = typeof b.ticket_id === 'number' && Number.isSafeInteger(b.ticket_id) && b.ticket_id >= 0 ? b.ticket_id : 0;
  const r = await tdm78ViaOrchestrator(Number(id), b.type, payload, ticketId);
  return Response.json(r, { status: r.ok ? 200 : r.status, headers: { 'cache-control': 'no-store' } });
}
