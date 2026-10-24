// TDM-78: server-only helpers for the robot page's command routes (/api/tdm78/robots/[id]/*).
// Never imported by client code: it reads server secrets.
//
// Two paths to the robot:
//   direct        Next server -> SpacetimeDB `issue_robot_command(robot, 'soft_estop', ...)` with the
//                 web service identity (RDF_WEB_SPACETIME_TOKEN, role `web`). The reducer accepts a
//                 soft_estop from ANY service role, so this skips the orchestrator entirely.
//   orchestrator  Next server -> orchestrator POST /robots/{id}/commands (X-RDF-Operator-Key =
//                 RDF_OPERATOR_KEY). Every motion command goes this way (only owner/orchestrator/
//                 factory may issue them); it is also the e-stop's fallback.
// Neither secret, nor the owner SPACETIME_TOKEN, ever reaches the browser.

import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { parseEnv } from 'node:util';

type Env = Record<string, string | undefined>;
let tdm78FileEnv: Env | null = null;

function tdm78DotEnv(): Env {
  if (tdm78FileEnv) return tdm78FileEnv;
  const candidates = [process.env.RDF_ENV_FILE, resolve(process.cwd(), '../../.env'), resolve(process.cwd(), '.env')];
  for (const f of candidates) {
    if (f && existsSync(f)) {
      try {
        tdm78FileEnv = parseEnv(readFileSync(f, 'utf8')) as Env;
        return tdm78FileEnv;
      } catch {
        /* unreadable: fall through */
      }
    }
  }
  tdm78FileEnv = {};
  return tdm78FileEnv;
}

/** process.env first, then the repo-root .env; blank counts as unset. Values are never logged. */
export function tdm78Env(...names: string[]): string | undefined {
  for (const n of names) {
    const v = process.env[n];
    if (v && v.trim()) return v.trim();
  }
  const file = tdm78DotEnv();
  for (const n of names) {
    const v = file[n];
    if (v && v.trim()) return v.trim();
  }
  return undefined;
}

export function tdm78SpacetimeTarget() {
  // Same database the browser subscribes to (lib/spacetime/config.ts).
  const host = (tdm78Env('NEXT_PUBLIC_SPACETIME_HOST', 'SPACETIME_HOST') || 'https://maincloud.spacetimedb.com').replace(/\/$/, '');
  const db = tdm78Env('NEXT_PUBLIC_SPACETIME_DB_NAME', 'SPACETIME_DB_NAME') || 'robo-factory-a706j';
  return { host, db, token: tdm78Env('RDF_WEB_SPACETIME_TOKEN') };
}

export function tdm78Orchestrator() {
  return (tdm78Env('RDF_ORCHESTRATOR_URL', 'NEXT_PUBLIC_ORCHESTRATOR_URL', 'ORCHESTRATOR_URL') || 'http://localhost:8000').replace(/\/$/, '');
}

export type Tdm78Result =
  | { ok: true; path: 'direct' | 'orchestrator'; elapsedMs: number; commandId?: number | null; seq?: number | null; detail?: string }
  | { ok: false; path: 'direct' | 'orchestrator'; status: number; elapsedMs: number; detail: string; unreachable?: boolean };

/** soft_estop straight into SpacetimeDB with the web service identity. */
export async function tdm78DirectEstop(robotId: number, reason: string): Promise<Tdm78Result | null> {
  const { host, db, token } = tdm78SpacetimeTarget();
  if (!token) return null; // not configured: caller falls back to the orchestrator
  const t0 = performance.now();
  try {
    const res = await fetch(`${host}/v1/database/${encodeURIComponent(db)}/call/issue_robot_command`, {
      method: 'POST',
      headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' },
      body: JSON.stringify([robotId, 'soft_estop', JSON.stringify({ reason, via: 'web:direct' }), 0]),
      cache: 'no-store',
      signal: AbortSignal.timeout(4_000),
    });
    const elapsedMs = Math.round(performance.now() - t0);
    if (res.ok) return { ok: true, path: 'direct', elapsedMs };
    return { ok: false, path: 'direct', status: res.status, elapsedMs, detail: (await res.text()).slice(0, 400) };
  } catch (e) {
    return { ok: false, path: 'direct', status: 502, elapsedMs: Math.round(performance.now() - t0), detail: (e as Error).message, unreachable: true };
  }
}

/** Any operator command through the orchestrator route (the key is added here, server side). */
export async function tdm78ViaOrchestrator(
  robotId: number,
  type: string,
  payload: Record<string, unknown>,
  ticketId: number,
  timeoutMs = 10_000,
): Promise<Tdm78Result> {
  const key = tdm78Env('RDF_OPERATOR_KEY');
  const t0 = performance.now();
  try {
    const res = await fetch(`${tdm78Orchestrator()}/robots/${robotId}/commands`, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'x-human-id': 'web-robot-page',
        ...(key ? { 'x-rdf-operator-key': key } : {}),
      },
      body: JSON.stringify({ type, payload, ticket_id: ticketId }),
      cache: 'no-store',
      signal: AbortSignal.timeout(timeoutMs),
    });
    const elapsedMs = Math.round(performance.now() - t0);
    const body = (await res.json().catch(() => ({}))) as { detail?: unknown; command_id?: number | null; seq?: number | null };
    if (res.ok) return { ok: true, path: 'orchestrator', elapsedMs, commandId: body.command_id ?? null, seq: body.seq ?? null };
    const detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail ?? res.statusText);
    return { ok: false, path: 'orchestrator', status: res.status, elapsedMs, detail };
  } catch (e) {
    return {
      ok: false,
      path: 'orchestrator',
      status: 502,
      elapsedMs: Math.round(performance.now() - t0),
      detail: `Orchestrator not reachable at ${tdm78Orchestrator()} (${(e as Error).message})`,
      unreachable: true,
    };
  }
}

export const TDM78_ROBOT_ID = /^\d{1,15}$/;
