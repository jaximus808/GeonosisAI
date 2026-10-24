// TDM-110: is the hybrid backend reachable from this web server? GET -> {mode, ok, error?, url?}
//   mode   'discover' (event_log orchestrator.endpoint + edge proof), 'env' (RDF_ORCHESTRATOR_URL), 'off'
//   url    the verified orchestrator URL, only for a logged-in operator (it is useless without the secret,
//          but there is no reason to hand it out)
// Forces a fresh look when the cached answer is older than the 10 s cache.

import { tdm110Resolver } from '@/lib/tdm110/edge';
import { operatorSessionOf } from '@/lib/operator/session';

export const dynamic = 'force-dynamic';
const NO_STORE = { 'cache-control': 'no-store' };

export async function GET(req: Request) {
  const r = tdm110Resolver();
  if (!r) {
    const env = process.env.RDF_ORCHESTRATOR_URL;
    return Response.json({ mode: env ? 'env' : 'off', ok: null }, { headers: NO_STORE });
  }
  let url: string | null = null;
  let error: string | null = null;
  try {
    url = await r.resolve();
  } catch (e) {
    error = (e as Error).message;
  }
  const op = operatorSessionOf(req) != null;
  return Response.json(
    { mode: 'discover', ok: Boolean(url), error, ...(op ? { url, event_id: r.state.lastId } : {}) },
    { status: url ? 200 : 503, headers: NO_STORE },
  );
}
