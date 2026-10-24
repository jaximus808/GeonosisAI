// TDM-55: server-side proxy for POST /stream/token on the orchestrator (its CORS is GET-only).
// Body {task_id, identity, spacetime_token} is passed through unchanged and never logged.
// Orchestrator base URL: RDF_ORCHESTRATOR_URL, else NEXT_PUBLIC_ORCHESTRATOR_URL, else http://localhost:8000.

import type { NextRequest } from 'next/server';

const ORCH = (
  process.env.RDF_ORCHESTRATOR_URL ||
  process.env.NEXT_PUBLIC_ORCHESTRATOR_URL ||
  'http://localhost:8000'
).replace(/\/$/, '');

export async function POST(req: NextRequest) {
  const b = (await req.json().catch(() => ({}))) as { task_id?: unknown; identity?: unknown; spacetime_token?: unknown };
  if (typeof b.task_id !== 'number' || typeof b.identity !== 'string' || typeof b.spacetime_token !== 'string') {
    return Response.json({ detail: 'task_id, identity and spacetime_token are required' }, { status: 422 });
  }
  try {
    const res = await fetch(`${ORCH}/stream/token`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ task_id: b.task_id, identity: b.identity, spacetime_token: b.spacetime_token }),
      cache: 'no-store',
      signal: AbortSignal.timeout(15_000),
    });
    return new Response(await res.text(), {
      status: res.status,
      headers: { 'content-type': res.headers.get('content-type') ?? 'application/json', 'cache-control': 'no-store' },
    });
  } catch (e) {
    return Response.json({ detail: `Orchestrator not reachable at ${ORCH} (${(e as Error).message})` }, { status: 502 });
  }
}
