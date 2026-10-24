// TDM-74: server-side proxy for the ticket-creation page (/tickets/new).
//
// Sibling of TDM-79's /api/tdm79 proxy. The orchestrator's CORS allows GET only, so the create
// POSTs go through the Next server. Only these two routes and these body keys are forwarded; the
// orchestrator does the real validation and its error body ({detail: {code, message, ...}} or
// FastAPI's {detail: [{loc, msg}]}) is passed back unchanged so the page can show it inline.
//
//   POST /api/tdm74/tickets/parse  -> POST /tickets/parse  (Claude reads the prompt; creates nothing)
//   POST /api/tdm74/tickets        -> POST /tickets        (send `parsed` back to skip a second parse)
//
// No token is involved: the orchestrator holds its own SpacetimeDB identity. Never put the owner
// token in the browser.
//
// Orchestrator base URL: RDF_ORCHESTRATOR_URL, else NEXT_PUBLIC_ORCHESTRATOR_URL, else http://localhost:8000.

import type { NextRequest } from 'next/server';

const TDM74_ORCH = (
  process.env.RDF_ORCHESTRATOR_URL ||
  process.env.NEXT_PUBLIC_ORCHESTRATOR_URL ||
  'http://localhost:8000'
).replace(/\/$/, '');

const TDM74_BODY_KEYS = new Set(['prompt', 'robot_id', 'settings_overrides', 'budget_cents', 'target_success', 'parsed']);
// create_ticket overrides (budget_cents is its own argument, not an override).
const TDM74_OVERRIDE_KEYS = new Set([
  'eval_trials',
  'success_threshold',
  'require_ci_low',
  'target_episodes',
  'max_iterations',
  'data_type_override',
  'hyperparams',
]);

type Ctx = { params: Promise<{ path: string[] }> };

function tdm74Json(status: number, body: unknown) {
  return Response.json(body, { status });
}

function tdm74Invalid(loc: string[], msg: string, type = 'invalid_body') {
  return tdm74Json(422, { detail: [{ loc, msg, type }] });
}

export async function POST(req: NextRequest, ctx: Ctx) {
  const parts = (await ctx.params).path;
  const target =
    parts.length === 1 && parts[0] === 'tickets'
      ? 'tickets'
      : parts.length === 2 && parts[0] === 'tickets' && parts[1] === 'parse'
        ? 'tickets/parse'
        : null;
  if (!target) return tdm74Json(404, { detail: 'not proxied' });

  const body = (await req.json().catch(() => null)) as Record<string, unknown> | null;
  if (!body || typeof body !== 'object' || Array.isArray(body)) return tdm74Invalid(['body'], 'send a JSON object');
  const extra = Object.keys(body).filter((k) => !TDM74_BODY_KEYS.has(k));
  if (extra.length) return tdm74Invalid(['body', extra[0]], `${extra[0]} is not a ticket field`, 'extra_forbidden');
  const ov = body.settings_overrides;
  if (ov !== undefined && ov !== null) {
    if (typeof ov !== 'object' || Array.isArray(ov)) return tdm74Invalid(['body', 'settings_overrides'], 'must be an object');
    const bad = Object.keys(ov).find((k) => !TDM74_OVERRIDE_KEYS.has(k));
    if (bad) return tdm74Invalid(['body', 'settings_overrides', bad], `${bad} is not a run setting`, 'extra_forbidden');
  }

  try {
    const res = await fetch(`${TDM74_ORCH}/${target}`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
      cache: 'no-store',
      signal: AbortSignal.timeout(90_000), // one Claude call
    });
    return new Response(await res.text(), {
      status: res.status,
      headers: { 'content-type': res.headers.get('content-type') ?? 'application/json', 'cache-control': 'no-store' },
    });
  } catch (e) {
    return tdm74Json(502, {
      detail: { code: 'orchestrator_unreachable', message: `Orchestrator not reachable at ${TDM74_ORCH} (${(e as Error).message})` },
    });
  }
}
