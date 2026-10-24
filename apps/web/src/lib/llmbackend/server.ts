// Server-only: the LLM backend proxy's upstream calls (orchestrator GET/PUT /admin/llm). It reads the
// operator key (RDF_OPERATOR_KEY), so client code never imports it.

import { tdm78Env, tdm78Orchestrator } from '@/lib/tdm78/server';
import { isProductionEnv, parseLlmBackendState, type LlmBackendState } from './types';

/** Production per this web server's env (process.env first, then the repo .env). */
export function webIsProduction(): boolean {
  return isProductionEnv({
    RDF_DEPLOY_ENV: tdm78Env('RDF_DEPLOY_ENV'),
    NEXT_PUBLIC_RDF_ENV: tdm78Env('NEXT_PUBLIC_RDF_ENV'),
    FLY_APP_NAME: tdm78Env('FLY_APP_NAME'),
  });
}

export type UpstreamResult =
  | { ok: true; status: number; state: LlmBackendState }
  | { ok: false; status: number; detail: string; unreachable?: boolean };

async function call(method: 'GET' | 'PUT', body?: unknown, by?: string): Promise<UpstreamResult> {
  const key = method === 'PUT' ? tdm78Env('RDF_OPERATOR_KEY') : undefined;
  const url = `${tdm78Orchestrator()}/admin/llm`;
  try {
    const res = await fetch(url, {
      method,
      headers: {
        'content-type': 'application/json',
        ...(key ? { 'x-rdf-operator-key': key } : {}),
        ...(by ? { 'x-rdf-human-id': by } : {}),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: 'no-store',
      signal: AbortSignal.timeout(8_000),
    });
    const b = (await res.json().catch(() => ({}))) as Record<string, unknown>;
    if (res.ok) {
      const state = parseLlmBackendState(b);
      if (state) return { ok: true, status: res.status, state };
      return { ok: false, status: 502, detail: 'orchestrator returned an unexpected /admin/llm body' };
    }
    const d = b.detail;
    return { ok: false, status: res.status, detail: typeof d === 'string' ? d : JSON.stringify(d ?? res.statusText) };
  } catch (e) {
    return {
      ok: false,
      status: 502,
      detail: `Orchestrator not reachable at ${tdm78Orchestrator()} (${(e as Error).message})`,
      unreachable: true,
    };
  }
}

export const getLlmBackend = () => call('GET');
export const putLlmBackend = (body: { backend: string; model: string | null; by: string }, by: string) =>
  call('PUT', body, by);
