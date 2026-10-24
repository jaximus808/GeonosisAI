// TDM-62: server-side proxy for the reset task page (/reset/[taskId]).
//
//   GET  /api/tdm62/resets/{id}        -> GET  /resets/{id}         reset routine + scene photos
//   POST /api/tdm62/resets/{id}/photo  -> POST /resets/{id}/photo   claimant attaches a scene photo
//        {identity, spacetime_token, source: "camera"|"upload", image_b64?}  (passed through, never logged)
//
// The orchestrator's CORS is GET-only, and the photo POST carries the browser's SpacetimeDB token
// (the orchestrator checks it is the task's claimant, like TDM-55's stream token).
// Orchestrator base URL: RDF_ORCHESTRATOR_URL, else NEXT_PUBLIC_ORCHESTRATOR_URL, else http://localhost:8000.

import type { NextRequest } from 'next/server';

const TDM62_ORCH = (
  process.env.RDF_ORCHESTRATOR_URL ||
  process.env.NEXT_PUBLIC_ORCHESTRATOR_URL ||
  'http://localhost:8000'
).replace(/\/$/, '');

const TDM62_ID = /^\d{1,19}$/;
const TDM62_MAX_BODY = 9_500_000; // ~6 MB photo as base64

type Ctx = { params: Promise<{ path: string[] }> };

function notProxied() {
  return Response.json({ detail: 'not proxied' }, { status: 404 });
}

async function forward(method: string, path: string, body?: string) {
  try {
    const res = await fetch(`${TDM62_ORCH}/${path}`, {
      method,
      headers: { 'content-type': 'application/json' },
      body,
      cache: 'no-store',
      signal: AbortSignal.timeout(30_000),
    });
    return new Response(await res.text(), {
      status: res.status,
      headers: { 'content-type': res.headers.get('content-type') ?? 'application/json', 'cache-control': 'no-store' },
    });
  } catch (e) {
    return Response.json({ detail: `Orchestrator not reachable at ${TDM62_ORCH} (${(e as Error).message})` }, { status: 502 });
  }
}

export async function GET(_req: NextRequest, ctx: Ctx) {
  const p = (await ctx.params).path;
  if (p.length !== 2 || p[0] !== 'resets' || !TDM62_ID.test(p[1])) return notProxied();
  return forward('GET', `resets/${p[1]}`);
}

export async function POST(req: NextRequest, ctx: Ctx) {
  const p = (await ctx.params).path;
  if (p.length !== 3 || p[0] !== 'resets' || !TDM62_ID.test(p[1]) || p[2] !== 'photo') return notProxied();
  const text = await req.text();
  if (text.length > TDM62_MAX_BODY) return Response.json({ detail: 'photo too large (max 6 MB)' }, { status: 413 });
  let b: { identity?: unknown; spacetime_token?: unknown; source?: unknown; image_b64?: unknown };
  try {
    b = JSON.parse(text);
  } catch {
    return Response.json({ detail: 'send JSON' }, { status: 422 });
  }
  if (typeof b.identity !== 'string' || typeof b.spacetime_token !== 'string') {
    return Response.json({ detail: 'identity and spacetime_token are required' }, { status: 422 });
  }
  const source = b.source === 'upload' ? 'upload' : 'camera';
  const out: Record<string, unknown> = { identity: b.identity, spacetime_token: b.spacetime_token, source };
  if (source === 'upload') out.image_b64 = typeof b.image_b64 === 'string' ? b.image_b64 : '';
  return forward('POST', `resets/${p[1]}/photo`, JSON.stringify(out));
}
