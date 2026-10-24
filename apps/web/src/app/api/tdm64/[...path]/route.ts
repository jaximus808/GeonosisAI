// TDM-64: server-side proxy from the browser to a short allow-list of orchestrator routes.
//
// The orchestrator's CORS allows GET only (app.py), and the earnings page / spend panel
// need POST (Stripe test-mode onboarding) and PATCH (raise a ticket budget). Proxying
// from the Next server keeps the orchestrator closed to every other route.
//
//   GET   /api/tdm64/payments/mode                          -> GET   /payments/mode
//   POST  /api/tdm64/payments/contributors/{id}/onboarding  -> POST  same (return_url, refresh_url)
//   POST  /api/tdm64/payments/contributors/{id}/refresh     -> POST  same
//   PATCH /api/tdm64/tickets/{id}/settings                  -> PATCH same, body {budget_cents} only,
//                                                              X-Human-Id passed through (logged as human:<id>)
//                                                              operator login required (spend change)
//
// Orchestrator base URL: RDF_ORCHESTRATOR_URL, else NEXT_PUBLIC_ORCHESTRATOR_URL, else http://localhost:8000.

import type { NextRequest } from 'next/server';
import { requireOperator } from '@/lib/operator/session';

const ORCH = (
  process.env.RDF_ORCHESTRATOR_URL ||
  process.env.NEXT_PUBLIC_ORCHESTRATOR_URL ||
  'http://localhost:8000'
).replace(/\/$/, '');

const HUMAN_ID = /^[A-Za-z0-9._@:+-]{1,64}$/;

type Ctx = { params: Promise<{ path: string[] }> };

function json(status: number, body: unknown) {
  return Response.json(body, { status });
}

async function forward(method: string, path: string, body?: string, headers: Record<string, string> = {}) {
  try {
    const res = await fetch(`${ORCH}/${path}`, {
      method,
      headers: { 'content-type': 'application/json', ...headers },
      body,
      cache: 'no-store',
      signal: AbortSignal.timeout(20_000),
    });
    const text = await res.text();
    return new Response(text, {
      status: res.status,
      headers: { 'content-type': res.headers.get('content-type') ?? 'application/json' },
    });
  } catch (e) {
    return json(502, { detail: `Orchestrator not reachable at ${ORCH} (${(e as Error).message})` });
  }
}

export async function GET(_req: NextRequest, ctx: Ctx) {
  const path = (await ctx.params).path.join('/');
  if (path === 'payments/mode') return forward('GET', path);
  return json(404, { detail: 'not proxied' });
}

export async function POST(req: NextRequest, ctx: Ctx) {
  const parts = (await ctx.params).path;
  const ok =
    parts.length === 4 &&
    parts[0] === 'payments' &&
    parts[1] === 'contributors' &&
    /^\d{1,19}$/.test(parts[2]) &&
    (parts[3] === 'onboarding' || parts[3] === 'refresh');
  if (!ok) return json(404, { detail: 'not proxied' });
  let body = '{}';
  if (parts[3] === 'onboarding') {
    const b = (await req.json().catch(() => ({}))) as { return_url?: unknown; refresh_url?: unknown };
    if (typeof b.return_url !== 'string' || typeof b.refresh_url !== 'string') {
      return json(422, { detail: 'return_url and refresh_url are required' });
    }
    body = JSON.stringify({ return_url: b.return_url, refresh_url: b.refresh_url });
  }
  return forward('POST', parts.join('/'), body);
}

export async function PATCH(req: NextRequest, ctx: Ctx) {
  const denied = requireOperator(req);
  if (denied) return denied;
  const parts = (await ctx.params).path;
  const ok = parts.length === 3 && parts[0] === 'tickets' && /^\d{1,19}$/.test(parts[1]) && parts[2] === 'settings';
  if (!ok) return json(404, { detail: 'not proxied' });
  const b = (await req.json().catch(() => ({}))) as { budget_cents?: unknown };
  if (typeof b.budget_cents !== 'number' || !Number.isSafeInteger(b.budget_cents) || b.budget_cents < 0) {
    return json(422, { detail: 'budget_cents must be a whole number of cents' });
  }
  const who = req.headers.get('x-human-id') ?? '';
  const headers: Record<string, string> = HUMAN_ID.test(who) ? { 'x-human-id': who } : {};
  return forward('PATCH', parts.join('/'), JSON.stringify({ budget_cents: b.budget_cents }), headers);
}
