// TDM-104: the ticket scene routes -> orchestrator scene_api (Python). Reads are open; generating, storing and
// deploying a scene are operator-only (lib/operator/session.ts) and carry RDF_OPERATOR_KEY, added here on the
// server (the browser never sees it).
//
//   GET  /api/tdm104/tickets/{id}/scene[?revision=N]   GET  /api/tdm104/robots/{id}/scene   GET /api/tdm104/scenes/default
//   POST /api/tdm104/tickets/{id}/scene/generate {hint?}          (operator)
//   PUT  /api/tdm104/tickets/{id}/scene {spec}                    (operator)
//   POST /api/tdm104/tickets/{id}/scene/deploy {revision?, robot_id?}  (operator)
//   POST /api/tdm104/scenes/validate {spec}

import type { NextRequest } from 'next/server';
import { requireOperator } from '@/lib/operator/session';
import { tdm78Env, tdm78Orchestrator } from '@/lib/tdm78/server';

type Ctx = { params: Promise<{ path: string[] }> };

const ID = '[1-9][0-9]{0,9}';
const ROUTES: { method: string; re: RegExp; operator: boolean; timeoutMs?: number }[] = [
  { method: 'GET', re: new RegExp(`^tickets/${ID}/scene$`), operator: false },
  { method: 'GET', re: new RegExp(`^robots/${ID}/scene$`), operator: false },
  { method: 'GET', re: /^scenes\/default$/, operator: false },
  { method: 'POST', re: /^scenes\/validate$/, operator: false },
  { method: 'POST', re: new RegExp(`^tickets/${ID}/scene/generate$`), operator: true, timeoutMs: 240_000 },
  { method: 'PUT', re: new RegExp(`^tickets/${ID}/scene$`), operator: true },
  { method: 'POST', re: new RegExp(`^tickets/${ID}/scene/deploy$`), operator: true },
];

async function proxy(req: NextRequest, ctx: Ctx, method: string): Promise<Response> {
  const { path } = await ctx.params;
  const p = (path ?? []).join('/');
  const route = ROUTES.find((r) => r.method === method && r.re.test(p));
  const headers = { 'cache-control': 'no-store' };
  if (!route) return Response.json({ ok: false, detail: `no such scene route: ${method} ${p}` }, { status: 404, headers });
  if (route.operator) {
    const denied = requireOperator(req);
    if (denied) return denied;
  }
  let body: string | undefined;
  if (method !== 'GET') {
    body = await req.text();
    if (body.length > 256_000) return Response.json({ ok: false, detail: 'body too large' }, { status: 413, headers });
    if (body && !(() => { try { JSON.parse(body); return true; } catch { return false; } })()) {
      return Response.json({ ok: false, detail: 'body must be JSON' }, { status: 400, headers });
    }
  }
  const qs = method === 'GET' ? new URL(req.url).searchParams.get('revision') : null;
  const url = `${tdm78Orchestrator()}/${p}${qs && /^[0-9]{1,9}$/.test(qs) ? `?revision=${qs}` : ''}`;
  const key = route.operator ? tdm78Env('RDF_OPERATOR_KEY') : undefined;
  try {
    const res = await fetch(url, {
      method,
      headers: {
        ...(body ? { 'content-type': 'application/json' } : {}),
        ...(route.operator ? { 'x-human-id': 'web-operator' } : {}),
        ...(key ? { 'x-rdf-operator-key': key } : {}),
      },
      body: body || (method === 'GET' ? undefined : '{}'),
      cache: 'no-store',
      signal: AbortSignal.timeout(route.timeoutMs ?? 15_000),
    });
    const text = await res.text();
    return new Response(text, { status: res.status, headers: { ...headers, 'content-type': 'application/json' } });
  } catch (e) {
    return Response.json(
      { ok: false, detail: `Orchestrator not reachable at ${tdm78Orchestrator()} (${(e as Error).message})`, unreachable: true },
      { status: 502, headers },
    );
  }
}

export const GET = (req: NextRequest, ctx: Ctx) => proxy(req, ctx, 'GET');
export const POST = (req: NextRequest, ctx: Ctx) => proxy(req, ctx, 'POST');
export const PUT = (req: NextRequest, ctx: Ctx) => proxy(req, ctx, 'PUT');
