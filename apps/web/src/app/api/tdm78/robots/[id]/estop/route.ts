// TDM-78: the robot page's soft e-stop. As direct as we can make it from a browser:
//
//   browser --POST--> this route (same origin, no auth needed: stopping is always allowed)
//           --> SpacetimeDB issue_robot_command(robot, 'soft_estop') as the `web` service identity
//           --> the robot-client's subscription sees the row and latches the stop at once.
//
// If the web identity isn't configured (RDF_WEB_SPACETIME_TOKEN) or that call fails, the same
// request falls back to the orchestrator route (POST /robots/{id}/commands, which never
// key-checks soft_estop). The answer says which path worked and how long it took.
//
// GET warms the server->SpacetimeDB connection (the page calls it on load, so the first press
// doesn't pay a TLS handshake) and reports which paths are configured. No secrets are returned.

import type { NextRequest } from 'next/server';
import { TDM78_ROBOT_ID, tdm78DirectEstop, tdm78SpacetimeTarget, tdm78ViaOrchestrator } from '@/lib/tdm78/server';

type Ctx = { params: Promise<{ id: string }> };

export async function POST(req: NextRequest, ctx: Ctx) {
  const { id } = await ctx.params;
  if (!TDM78_ROBOT_ID.test(id)) return Response.json({ ok: false, detail: 'bad robot id' }, { status: 400 });
  const robotId = Number(id);
  const body = (await req.json().catch(() => ({}))) as { reason?: unknown };
  const reason = typeof body.reason === 'string' ? body.reason.slice(0, 200) : 'robot page soft e-stop';
  const t0 = performance.now();

  const attempts = [];
  const direct = await tdm78DirectEstop(robotId, reason);
  if (direct) {
    attempts.push(direct);
    if (direct.ok) {
      return Response.json({ ...direct, totalMs: Math.round(performance.now() - t0), attempts }, { headers: { 'cache-control': 'no-store' } });
    }
  }
  const viaOrch = await tdm78ViaOrchestrator(robotId, 'soft_estop', { reason, via: 'web:orchestrator' }, 0, 5_000);
  attempts.push(viaOrch);
  const status = viaOrch.ok ? 200 : 502;
  return Response.json({ ...viaOrch, totalMs: Math.round(performance.now() - t0), attempts }, { status, headers: { 'cache-control': 'no-store' } });
}

export async function GET() {
  const { host, token } = tdm78SpacetimeTarget();
  let warm = false;
  if (token) {
    try {
      const r = await fetch(`${host}/v1/ping`, { cache: 'no-store', signal: AbortSignal.timeout(3_000) });
      warm = r.ok;
    } catch {
      warm = false;
    }
  }
  return Response.json({ direct: Boolean(token), directWarm: warm, fallback: 'orchestrator' }, { headers: { 'cache-control': 'no-store' } });
}
