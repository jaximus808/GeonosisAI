// TDM-78: operator commands from the robot page (Home, Stop, Run baseline, Load checkpoint,
// Start/Stop teleop) -> orchestrator POST /robots/{id}/commands, which issues them under the
// orchestrator's identity. The operator key (RDF_OPERATOR_KEY) is added on the server; the
// browser never sees it. soft_estop has its own, more direct route (../estop).
//
// Auth gap (see docs/decisions.md, TDM-78): the web app has no operator login, so anyone who can
// load this site can press these buttons. The key only stops callers that skip this server.

import type { NextRequest } from 'next/server';
import { TDM78_ROBOT_ID, tdm78ViaOrchestrator } from '@/lib/tdm78/server';

const TDM78_TYPES = new Set(['home', 'stop', 'run_baseline', 'load_checkpoint', 'start_teleop', 'stop_teleop']);

type Ctx = { params: Promise<{ id: string }> };

export async function POST(req: NextRequest, ctx: Ctx) {
  const { id } = await ctx.params;
  if (!TDM78_ROBOT_ID.test(id)) return Response.json({ ok: false, detail: 'bad robot id' }, { status: 400 });
  const b = (await req.json().catch(() => null)) as { type?: unknown; payload?: unknown; ticket_id?: unknown } | null;
  if (!b || typeof b.type !== 'string' || !TDM78_TYPES.has(b.type)) {
    return Response.json({ ok: false, detail: `type must be one of ${[...TDM78_TYPES].join(', ')}` }, { status: 400 });
  }
  const payload = b.payload && typeof b.payload === 'object' && !Array.isArray(b.payload) ? (b.payload as Record<string, unknown>) : {};
  if (JSON.stringify(payload).length > 4096) return Response.json({ ok: false, detail: 'payload too large' }, { status: 413 });
  if (b.type === 'load_checkpoint' && typeof payload.checkpoint_uri !== 'string') {
    return Response.json({ ok: false, detail: 'load_checkpoint needs payload.checkpoint_uri' }, { status: 400 });
  }
  const ticketId = typeof b.ticket_id === 'number' && Number.isSafeInteger(b.ticket_id) && b.ticket_id >= 0 ? b.ticket_id : 0;
  const r = await tdm78ViaOrchestrator(Number(id), b.type, payload, ticketId);
  return Response.json(r, { status: r.ok ? 200 : r.status, headers: { 'cache-control': 'no-store' } });
}
