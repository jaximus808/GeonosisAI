// Public mode (make demo-public): the browser's orchestrator GETs, through the Next server.
//
// Components fetch `${NEXT_PUBLIC_ORCHESTRATOR_URL}/<path>`. With NEXT_PUBLIC_ORCHESTRATOR_URL=/api/orch those
// become same-origin calls here, so a phone needs no tunnel to the orchestrator and the orchestrator stays on
// 127.0.0.1 (most of its routes have no auth of their own). Only this GET allow-list is forwarded:
//
//   GET /api/orch/media/sign?uri=...         -> GET /media/sign?uri=...        (TDM-75 / TDM-68 video URLs)
//   GET /api/orch/tickets/{id}/decision      -> GET /tickets/{id}/decision     (TDM-68 evidence trail)
//   GET /api/orch/agents/replay/source       -> GET /agents/replay/source      (agents panel replay)
//
// Orchestrator base URL: RDF_ORCHESTRATOR_URL (server only). Never NEXT_PUBLIC_ORCHESTRATOR_URL here: in
// public mode that is this route itself.

import type { NextRequest } from 'next/server';
import { orchProxyTarget } from '@/lib/public/orchProxy';

const ORCH_PROXY_BASE = (process.env.RDF_ORCHESTRATOR_URL || 'http://localhost:8000').replace(/\/$/, '');

type Ctx = { params: Promise<{ path: string[] }> };

export async function GET(req: NextRequest, ctx: Ctx) {
  const target = orchProxyTarget((await ctx.params).path, req.nextUrl.searchParams);
  if (!target) return Response.json({ detail: 'not proxied' }, { status: 404, headers: { 'cache-control': 'no-store' } });
  try {
    const res = await fetch(`${ORCH_PROXY_BASE}/${target}`, { cache: 'no-store', signal: AbortSignal.timeout(20_000) });
    return new Response(await res.text(), {
      status: res.status,
      headers: { 'content-type': res.headers.get('content-type') ?? 'application/json', 'cache-control': 'no-store' },
    });
  } catch (e) {
    return Response.json({ detail: `Orchestrator not reachable (${(e as Error).message})` }, { status: 502 });
  }
}
