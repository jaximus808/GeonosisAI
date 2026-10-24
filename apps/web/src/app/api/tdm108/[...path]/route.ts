// TDM-108: proxy for the job-drop routes on the orchestrator (services/orchestrator/.../job_drops_api.py).
//
//   GET  /api/tdm108/drops                          -> GET  /drops                  (operator login)
//   GET  /api/tdm108/drops/{id}/card/{ch}.png       -> GET  same                    (operator login)
//   POST /api/tdm108/drops/{id}/post  {channel}     -> POST same                    (operator login)
//   POST /api/tdm108/drops/preview    {task_id}     -> POST same                    (operator login)
//   GET  /api/tdm108/share/{task}.png?size=&download=1 -> GET /drops/share/{task}.png (public: the Share button)
//   POST /api/tdm108/claim-src        {task_id, src, identity, spacetime_token} -> POST /drops/claim-src (public;
//                                     the orchestrator checks the browser's own SpacetimeDB identity)
//
// Operator routes add the orchestrator's X-RDF-Operator-Key on the server after requireOperator().
// The key never reaches the browser. Only this allow-list is forwarded.

import type { NextRequest } from 'next/server';
import { requireOperator } from '@/lib/operator/session';
import { tdm78Env, tdm78Orchestrator } from '@/lib/tdm78/server';
import { tdm108Route } from '@/lib/tdm108/proxy';

type Ctx = { params: Promise<{ path: string[] }> };

async function tdm108Forward(
  method: string,
  path: string,
  init: { body?: string; operator?: boolean; download?: string } = {},
): Promise<Response> {
  const key = init.operator ? tdm78Env('RDF_OPERATOR_KEY') : undefined;
  try {
    const res = await fetch(`${tdm78Orchestrator()}/${path}`, {
      method,
      headers: { 'content-type': 'application/json', ...(key ? { 'x-rdf-operator-key': key } : {}) },
      body: init.body,
      cache: 'no-store',
      signal: AbortSignal.timeout(30_000),
    });
    const headers: Record<string, string> = {
      'content-type': res.headers.get('content-type') ?? 'application/json',
      'cache-control': init.operator ? 'no-store' : (res.headers.get('cache-control') ?? 'no-store'),
    };
    if (init.download && res.ok) headers['content-disposition'] = `attachment; filename="${init.download}"`;
    return new Response(await res.arrayBuffer(), { status: res.status, headers });
  } catch (e) {
    return Response.json({ detail: `Orchestrator not reachable (${(e as Error).name})` }, { status: 502 });
  }
}

export async function GET(req: NextRequest, ctx: Ctx) {
  const r = tdm108Route('GET', (await ctx.params).path, req.nextUrl.searchParams);
  if (!r) return Response.json({ detail: 'not proxied' }, { status: 404 });
  if (r.operator) {
    const denied = requireOperator(req);
    if (denied) return denied;
  }
  return tdm108Forward('GET', r.target, { operator: r.operator, download: r.download });
}

export async function POST(req: NextRequest, ctx: Ctx) {
  const r = tdm108Route('POST', (await ctx.params).path, req.nextUrl.searchParams);
  if (!r) return Response.json({ detail: 'not proxied' }, { status: 404 });
  if (r.operator) {
    const denied = requireOperator(req);
    if (denied) return denied;
  }
  const raw = await req.text();
  if (raw.length > 16_384) return Response.json({ detail: 'body too large' }, { status: 413 });
  return tdm108Forward('POST', r.target, { body: raw || '{}', operator: r.operator });
}
