// LLM backend switch (local/dev only): which model backend the orchestrator's Claude calls use
// (diagnose, planner, reviewer, LLM QA, ticket parsing). Proxies orchestrator GET/PUT /admin/llm.
//
//   GET  /api/llm-backend   current backend, allowed backends, env, locked. Open: it is read-only and
//                           carries no secret (the header chip shows it to everyone in dev).
//   PUT  /api/llm-backend   {backend: "claude_code"|"droid"|"api", model?: string|null}
//                           Operator login required (requireOperator); the operator key is added here.
//
// Production lock: when this web server's env says production (RDF_DEPLOY_ENV / NEXT_PUBLIC_RDF_ENV =
// production|prod|cloud, or FLY_APP_NAME), PUT is refused with 403 before anything is forwarded and GET
// reports locked. The orchestrator enforces the same lock itself; this is the second line.

import type { NextRequest } from 'next/server';
import { requireOperator } from '@/lib/operator/session';
import { getLlmBackend, putLlmBackend, webIsProduction } from '@/lib/llmbackend/server';
import { isLlmBackend, LLM_BACKENDS } from '@/lib/llmbackend/types';

const NO_STORE = { 'cache-control': 'no-store' };
const LOCKED = 'LLM backend is locked in production (api)';

export async function GET() {
  const r = await getLlmBackend();
  if (!r.ok) return Response.json({ ok: false, detail: r.detail, unreachable: r.unreachable ?? false }, { status: r.status, headers: NO_STORE });
  const state = webIsProduction() ? { ...r.state, locked: true, allowed: ['api'] } : r.state;
  return Response.json({ ok: true, ...state }, { headers: NO_STORE });
}

export async function PUT(req: NextRequest) {
  if (webIsProduction()) return Response.json({ ok: false, code: 'locked', detail: LOCKED }, { status: 403, headers: NO_STORE });
  const denied = requireOperator(req);
  if (denied) return denied;
  const b = (await req.json().catch(() => null)) as { backend?: unknown; model?: unknown } | null;
  if (!b || !isLlmBackend(b.backend)) {
    return Response.json({ ok: false, detail: `backend must be one of ${LLM_BACKENDS.join(', ')}` }, { status: 400, headers: NO_STORE });
  }
  if (b.model != null && (typeof b.model !== 'string' || b.model.length > 120 || !/^[\w.:@/-]*$/.test(b.model))) {
    return Response.json({ ok: false, detail: 'model must be a short model id (letters, digits, . _ - : @ /)' }, { status: 400, headers: NO_STORE });
  }
  const model = typeof b.model === 'string' && b.model.trim() ? b.model.trim() : null;
  const by = 'web:ops-settings';
  const r = await putLlmBackend({ backend: b.backend, model, by }, by);
  if (!r.ok) return Response.json({ ok: false, detail: r.detail, unreachable: r.unreachable ?? false }, { status: r.status, headers: NO_STORE });
  return Response.json({ ok: true, ...r.state }, { headers: NO_STORE });
}
