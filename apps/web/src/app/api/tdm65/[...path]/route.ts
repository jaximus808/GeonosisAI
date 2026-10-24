// TDM-65: server-side proxy for the orchestrator's web-push routes (its CORS is GET-only, and the
// phone may reach this app through a tunnel that can't see the orchestrator's port).
//
//   GET  /api/tdm65/config                        -> GET  /push/config   (VAPID public key only)
//   POST /api/tdm65/{subscribe,unsubscribe,state,test} -> POST /push/...
//
// POST bodies carry this browser's own SpacetimeDB identity token; the orchestrator verifies it with
// SpacetimeDB. Bodies (token, push endpoint) are passed through and never logged.
// Orchestrator base URL: RDF_ORCHESTRATOR_URL, else NEXT_PUBLIC_ORCHESTRATOR_URL, else http://localhost:8000.

import type { NextRequest } from 'next/server';

const TDM65_ORCH = (
  process.env.RDF_ORCHESTRATOR_URL ||
  process.env.NEXT_PUBLIC_ORCHESTRATOR_URL ||
  'http://localhost:8000'
).replace(/\/$/, '');
const TDM65_POST = new Set(['subscribe', 'unsubscribe', 'state', 'test']);
const TDM65_MAX_BODY = 16_000;

type Ctx = { params: Promise<{ path: string[] }> };

function tdm65Json(status: number, detail: unknown) {
  return Response.json({ detail }, { status, headers: { 'cache-control': 'no-store' } });
}

async function tdm65Forward(method: 'GET' | 'POST', path: string, body?: string) {
  try {
    const res = await fetch(`${TDM65_ORCH}/push/${path}`, {
      method,
      headers: body ? { 'content-type': 'application/json' } : undefined,
      body,
      cache: 'no-store',
      signal: AbortSignal.timeout(15_000),
    });
    return new Response(await res.text(), {
      status: res.status,
      headers: { 'content-type': res.headers.get('content-type') ?? 'application/json', 'cache-control': 'no-store' },
    });
  } catch (e) {
    return tdm65Json(502, `Orchestrator not reachable at ${TDM65_ORCH} (${(e as Error).message})`);
  }
}

export async function GET(_req: NextRequest, ctx: Ctx) {
  const parts = (await ctx.params).path;
  if (parts.length === 1 && parts[0] === 'config') return tdm65Forward('GET', 'config');
  return tdm65Json(404, 'not proxied');
}

export async function POST(req: NextRequest, ctx: Ctx) {
  const parts = (await ctx.params).path;
  if (parts.length !== 1 || !TDM65_POST.has(parts[0])) return tdm65Json(404, 'not proxied');
  const text = await req.text();
  if (text.length > TDM65_MAX_BODY) return tdm65Json(413, 'body too large');
  try {
    const b = JSON.parse(text || '{}');
    if (!b || typeof b !== 'object' || Array.isArray(b)) throw new Error('not an object');
  } catch {
    return tdm65Json(422, 'Send a JSON object.');
  }
  return tdm65Forward('POST', parts[0], text);
}
