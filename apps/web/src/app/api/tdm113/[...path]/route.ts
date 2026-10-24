// TDM-113: proxy for the orchestrator's text job offer routes (services/orchestrator/.../text_offers_api.py).
//
//   POST /api/tdm113/{optin,state,resend,optout} -> POST /texts/...   (public: the body carries this browser's own
//                                                   SpacetimeDB identity token; the orchestrator verifies it)
//   GET  /api/tdm113/status                      -> GET  /texts/status (operator login; counts + masked outbox)
//
// Bodies (token, phone number) are passed through and never logged. Only this allow-list is forwarded.

import type { NextRequest } from 'next/server';
import { requireOperator } from '@/lib/operator/session';
import { tdm78Env, tdm78Orchestrator } from '@/lib/tdm78/server';
import { tdm113Route } from '@/lib/tdm113/texts';

type Ctx = { params: Promise<{ path: string[] }> };

async function tdm113Forward(method: 'GET' | 'POST', target: string, init: { body?: string; operator?: boolean } = {}) {
  const key = init.operator ? tdm78Env('RDF_OPERATOR_KEY') : undefined;
  try {
    const res = await fetch(`${tdm78Orchestrator()}/${target}`, {
      method,
      headers: { 'content-type': 'application/json', ...(key ? { 'x-rdf-operator-key': key } : {}) },
      body: init.body,
      cache: 'no-store',
      signal: AbortSignal.timeout(20_000),
    });
    return new Response(await res.text(), {
      status: res.status,
      headers: { 'content-type': res.headers.get('content-type') ?? 'application/json', 'cache-control': 'no-store' },
    });
  } catch (e) {
    return Response.json({ detail: `Orchestrator not reachable (${(e as Error).name})` }, { status: 502 });
  }
}

export async function GET(req: NextRequest, ctx: Ctx) {
  const r = tdm113Route('GET', (await ctx.params).path);
  if (!r) return Response.json({ detail: 'not proxied' }, { status: 404 });
  const denied = requireOperator(req);
  if (denied) return denied;
  return tdm113Forward('GET', r.target, { operator: true });
}

export async function POST(req: NextRequest, ctx: Ctx) {
  const r = tdm113Route('POST', (await ctx.params).path);
  if (!r) return Response.json({ detail: 'not proxied' }, { status: 404 });
  const raw = await req.text();
  if (raw.length > 16_000) return Response.json({ detail: 'body too large' }, { status: 413 });
  try {
    const b = JSON.parse(raw || '{}');
    if (!b || typeof b !== 'object' || Array.isArray(b)) throw new Error('not an object');
  } catch {
    return Response.json({ detail: 'Send a JSON object.' }, { status: 422 });
  }
  return tdm113Forward('POST', r.target, { body: raw });
}
