// TDM-114: server-side forwarding to the orchestrator's arbitration routes. The operator key
// (RDF_OPERATOR_KEY) is added here, never in the browser. Server only.
import { tdm78Env, tdm78Orchestrator } from '@/lib/tdm78/server';

export const TDM114_ID = /^\d{1,19}$/;
const TDM114_HUMAN_ID = /^[A-Za-z0-9._@:+-]{1,64}$/;

export async function tdm114Forward(path: string, init: { method: 'GET' | 'POST'; operator?: boolean; by?: string | null }) {
  const headers: Record<string, string> = { 'content-type': 'application/json' };
  if (init.operator) {
    const key = tdm78Env('RDF_OPERATOR_KEY');
    if (!key) return Response.json({ detail: { code: 'no_operator_key', message: 'RDF_OPERATOR_KEY is not set on the web server' } }, { status: 503 });
    headers['x-rdf-operator-key'] = key;
  }
  const by = init.by && TDM114_HUMAN_ID.test(init.by) ? init.by : 'web-operator';
  try {
    const res = await fetch(`${tdm78Orchestrator()}/${path}`, {
      method: init.method,
      headers,
      body: init.method === 'POST' ? JSON.stringify({ by }) : undefined,
      cache: 'no-store',
      signal: AbortSignal.timeout(20_000),
    });
    return new Response(await res.text(), {
      status: res.status,
      headers: { 'content-type': res.headers.get('content-type') ?? 'application/json', 'cache-control': 'no-store' },
    });
  } catch (e) {
    return Response.json(
      { detail: { code: 'orchestrator_unreachable', message: `Orchestrator not reachable at ${tdm78Orchestrator()} (${(e as Error).message})` } },
      { status: 502 },
    );
  }
}
