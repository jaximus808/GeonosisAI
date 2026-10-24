// TDM-57: the robot page's "Revoke control" -> orchestrator POST /tasks/{id}/revoke, which calls
// release_task (default) or cancel_task (cancel: true) as the orchestrator. The operator key
// (RDF_OPERATOR_KEY) is added here on the server; the browser never sees it.
//
// Same auth gap as TDM-78 (docs/decisions.md): the dashboard has no operator login, so anyone who can
// load the site can press it. The key only stops callers that skip this server.

import type { NextRequest } from 'next/server';
import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { parseEnv } from 'node:util';

type Ctx = { params: Promise<{ id: string }> };

function tdm57Env(...names: string[]): string | undefined {
  for (const n of names) {
    const v = process.env[n];
    if (v && v.trim()) return v.trim();
  }
  for (const f of [process.env.RDF_ENV_FILE, resolve(process.cwd(), '../../.env'), resolve(process.cwd(), '.env')]) {
    if (!f || !existsSync(f)) continue;
    try {
      const env = parseEnv(readFileSync(f, 'utf8')) as Record<string, string | undefined>;
      for (const n of names) {
        const v = env[n];
        if (v && v.trim()) return v.trim();
      }
      return undefined;
    } catch {
      /* unreadable: try the next */
    }
  }
  return undefined;
}

export async function POST(req: NextRequest, ctx: Ctx) {
  const { id } = await ctx.params;
  if (!/^\d{1,15}$/.test(id)) return Response.json({ detail: 'bad task id' }, { status: 400 });
  const b = (await req.json().catch(() => ({}))) as { reason?: unknown; cancel?: unknown };
  const reason = typeof b.reason === 'string' ? b.reason.slice(0, 500) : 'operator revoke';
  const orch = (tdm57Env('RDF_ORCHESTRATOR_URL', 'NEXT_PUBLIC_ORCHESTRATOR_URL', 'ORCHESTRATOR_URL') || 'http://localhost:8000').replace(/\/$/, '');
  const key = tdm57Env('RDF_OPERATOR_KEY');
  try {
    const res = await fetch(`${orch}/tasks/${id}/revoke`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'x-human-id': 'web-robot-page', ...(key ? { 'x-rdf-operator-key': key } : {}) },
      body: JSON.stringify({ reason, cancel: b.cancel === true }),
      cache: 'no-store',
      signal: AbortSignal.timeout(10_000),
    });
    return new Response(await res.text(), {
      status: res.status,
      headers: { 'content-type': res.headers.get('content-type') ?? 'application/json', 'cache-control': 'no-store' },
    });
  } catch (e) {
    return Response.json({ detail: `Orchestrator not reachable at ${orch} (${(e as Error).message})` }, { status: 502 });
  }
}
