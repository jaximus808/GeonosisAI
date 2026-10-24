// TDM-79: server-side proxy for the run-settings editor and the report page.
//
// Sibling of TDM-64's /api/tdm64 proxy (which only forwards {budget_cents}). The orchestrator's
// CORS allows GET only, so the full settings PATCH and the report save go through the Next server.
// Only this allow-list is forwarded; the orchestrator does the strict validation (TDM-20 field-level
// 422s) and its error body is passed back unchanged so the editor can show it per field.
//
//   GET   /api/tdm79/tickets/{id}/settings     -> GET   /tickets/{id}/settings
//   PATCH /api/tdm79/tickets/{id}/settings     -> PATCH same (known keys only; X-Human-Id passed through)
//   GET   /api/tdm79/tickets/{id}/report       -> GET   /tickets/{id}/report
//   GET   /api/tdm79/tickets/{id}/report.md    -> GET   same, as a download (Content-Disposition)
//   POST  /api/tdm79/tickets/{id}/report/save  -> POST  same (stores .md + .json, logs report.saved)
//
// PATCH and POST need an operator login (lib/operator/session.ts); the GETs stay public.
//
// No token is involved: the orchestrator holds its own SpacetimeDB identity. Never put the owner
// token in the browser.
//
// Orchestrator base URL: RDF_ORCHESTRATOR_URL, else NEXT_PUBLIC_ORCHESTRATOR_URL, else http://localhost:8000.

import type { NextRequest } from 'next/server';
import { requireOperator } from '@/lib/operator/session';

const TDM79_ORCH = (
  process.env.RDF_ORCHESTRATOR_URL ||
  process.env.NEXT_PUBLIC_ORCHESTRATOR_URL ||
  'http://localhost:8000'
).replace(/\/$/, '');

const TDM79_HUMAN_ID = /^[A-Za-z0-9._@:+-]{1,64}$/;
const TDM79_TICKET = /^\d{1,19}$/;
const TDM79_SETTINGS_KEYS = new Set([
  'eval_trials',
  'success_threshold',
  'require_ci_low',
  'budget_cents',
  'target_episodes',
  'max_iterations',
  'data_type_override',
  'hyperparams',
]);

type Ctx = { params: Promise<{ path: string[] }> };

function tdm79Json(status: number, body: unknown) {
  return Response.json(body, { status });
}

function tdm79NotProxied() {
  return tdm79Json(404, { detail: 'not proxied' });
}

async function tdm79Forward(
  method: string,
  path: string,
  init: { body?: string; headers?: Record<string, string>; download?: string } = {},
) {
  try {
    const res = await fetch(`${TDM79_ORCH}/${path}`, {
      method,
      headers: { 'content-type': 'application/json', ...(init.headers ?? {}) },
      body: init.body,
      cache: 'no-store',
      signal: AbortSignal.timeout(30_000),
    });
    const headers: Record<string, string> = {
      'content-type': res.headers.get('content-type') ?? 'application/json',
      'cache-control': 'no-store',
    };
    if (init.download && res.ok) headers['content-disposition'] = `attachment; filename="${init.download}"`;
    return new Response(await res.text(), { status: res.status, headers });
  } catch (e) {
    return tdm79Json(502, {
      detail: [{ loc: [], msg: `Orchestrator not reachable at ${TDM79_ORCH} (${(e as Error).message})`, type: 'orchestrator_unreachable' }],
    });
  }
}

function ticketPath(parts: string[]): string | null {
  return parts.length >= 3 && parts[0] === 'tickets' && TDM79_TICKET.test(parts[1]) ? parts[1] : null;
}

export async function GET(_req: NextRequest, ctx: Ctx) {
  const parts = (await ctx.params).path;
  const id = ticketPath(parts);
  if (!id || parts.length !== 3) return tdm79NotProxied();
  if (parts[2] === 'settings' || parts[2] === 'report') return tdm79Forward('GET', parts.join('/'));
  if (parts[2] === 'report.md') {
    return tdm79Forward('GET', parts.join('/'), { download: `ticket-${id}-report.md` });
  }
  return tdm79NotProxied();
}

export async function PATCH(req: NextRequest, ctx: Ctx) {
  const denied = requireOperator(req);
  if (denied) return denied;
  const parts = (await ctx.params).path;
  if (!ticketPath(parts) || parts.length !== 3 || parts[2] !== 'settings') return tdm79NotProxied();
  const body = (await req.json().catch(() => null)) as unknown;
  if (!body || typeof body !== 'object' || Array.isArray(body)) {
    return tdm79Json(422, { detail: [{ loc: ['body'], msg: 'send a JSON object of settings', type: 'invalid_body' }] });
  }
  const unknown = Object.keys(body).filter((k) => !TDM79_SETTINGS_KEYS.has(k));
  if (unknown.length) {
    return tdm79Json(422, {
      detail: unknown.map((k) => ({ loc: ['body', k], msg: `${k} is not a run setting`, type: 'extra_forbidden' })),
    });
  }
  const who = req.headers.get('x-human-id') ?? '';
  const headers: Record<string, string> = TDM79_HUMAN_ID.test(who) ? { 'x-human-id': who } : {};
  return tdm79Forward('PATCH', parts.join('/'), { body: JSON.stringify(body), headers });
}

export async function POST(req: NextRequest, ctx: Ctx) {
  const denied = requireOperator(req);
  if (denied) return denied;
  const parts = (await ctx.params).path;
  const ok = ticketPath(parts) && parts.length === 4 && parts[2] === 'report' && parts[3] === 'save';
  if (!ok) return tdm79NotProxied();
  return tdm79Forward('POST', parts.join('/'), { body: '{}' });
}
