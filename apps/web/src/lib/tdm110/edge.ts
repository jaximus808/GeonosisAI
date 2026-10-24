// TDM-110 hybrid deploy: the web app on Vercel, the orchestrator on a laptop/VM behind a cloudflared QUICK
// tunnel whose random URL changes on every backend restart. Server only (reads RDF_EDGE_SECRET).
//
// How the web server finds the orchestrator (no redeploy, no env change when the tunnel URL rotates):
//   1. `make demo-hybrid` (scripts/demo/hybrid.py) publishes event_log `orchestrator.endpoint`
//      {url, robot_url, ...} (or {url: null, state: "down"} on stop). append_event refuses anonymous callers
//      and a non-orchestrator service may not log as actor `orchestrator`, so only the orchestrator (or the
//      owner) can write a row this code accepts (actor === 'orchestrator').
//   2. Here: the newest such row, read over SpacetimeDB HTTP SQL, cached 10 s.
//   3. Before ANY request carries the secret, the candidate must answer GET <url>/_rdf/proof?nonce=<random>
//      with HMAC-SHA256(RDF_EDGE_SECRET, "rdf-edge-proof-v1\n<nonce>\n<url>") (scripts/demo/public_guard.py
//      edge_proof). Binding the URL defeats a relay; a fresh nonce defeats replay. Re-proved every 5 min.
//   4. Requests then carry `x-rdf-edge-secret`; the edge refuses the orchestrator without it.
//
// The many existing server routes keep their own `RDF_ORCHESTRATOR_URL || ... || 'http://localhost:8000'`
// bases; instrumentation.ts installs tdm110InstallFetchHook(), which rewrites fetches aimed at the discovery
// sentinel (or, on Vercel, at the unset-env fallbacks) to the discovered URL and adds the secret. An explicit
// real RDF_ORCHESTRATOR_URL still wins (the secret is added to it when RDF_EDGE_SECRET is set).
//
// Pure apart from node:crypto and the fetch passed in, so test/tdm110-hybrid.test.ts runs it under node.

import { createHmac, randomBytes, timingSafeEqual } from 'node:crypto';

export const TDM110_SENTINEL = 'https://rdf-orchestrator.invalid';
export const TDM110_SECRET_HEADER = 'x-rdf-edge-secret';
export const TDM110_EVENT = 'orchestrator.endpoint';
export const TDM110_ACTOR = 'orchestrator';
export const TDM110_CACHE_MS = 10_000;
export const TDM110_REPROVE_MS = 5 * 60_000;
const PROOF_LABEL = 'rdf-edge-proof-v1';

type Env = Record<string, string | undefined>;
type Fetch = typeof fetch;

export function tdm110Proof(secret: string, nonce: string, url: string): string {
  return createHmac('sha256', secret).update(`${PROOF_LABEL}\n${nonce}\n${url.replace(/\/+$/, '')}`).digest('hex');
}

function sameHex(a: string, b: string): boolean {
  const x = Buffer.from(a, 'utf8');
  const y = Buffer.from(b, 'utf8');
  return x.length === y.length && timingSafeEqual(x, y);
}

/** The newest acceptable `orchestrator.endpoint` row among SQL rows ({id, actor, type, payload_json}). */
export function tdm110PickEndpoint(
  rows: Record<string, unknown>[],
): { id: number; url: string | null; robotUrl: string | null } | null {
  let best: { id: number; url: string | null; robotUrl: string | null } | null = null;
  for (const r of rows) {
    if (r.type !== TDM110_EVENT || r.actor !== TDM110_ACTOR) continue;
    const id = Number(r.id);
    if (!Number.isFinite(id) || (best && id <= best.id)) continue;
    let p: { url?: unknown; robot_url?: unknown } = {};
    try {
      p = JSON.parse(String(r.payload_json ?? '{}'));
    } catch {
      continue;
    }
    const raw = typeof p.url === 'string' ? p.url.replace(/\/+$/, '') : '';
    // an https origin (a tunnel), or plain http on loopback (local tests); never a path
    const url = /^https:\/\/[a-z0-9.-]+(:\d+)?$/i.test(raw) || /^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(raw) ? raw : null;
    const robotUrl = typeof p.robot_url === 'string' ? p.robot_url : null;
    best = { id, url, robotUrl };
  }
  return best;
}

/** SpacetimeDB HTTP SQL result -> row objects. */
export function tdm110SqlRows(body: unknown): Record<string, unknown>[] {
  const [out] = (Array.isArray(body) ? body : []) as { schema?: { elements?: { name?: { some?: string } }[] }; rows?: unknown[][] }[];
  if (!out?.schema?.elements || !Array.isArray(out.rows)) return [];
  const names = out.schema.elements.map((e) => e.name?.some ?? '');
  return out.rows.map((r) => Object.fromEntries(names.map((n, i) => [n, r[i]])));
}

export type Tdm110Config = {
  secret: string;
  stdbHost: string; // SpacetimeDB HTTP base the server reads event_log from
  db: string;
  stdbViaEdge: boolean; // test only: the DB sits behind the edge's /v1 (needs the secret for /sql)
};

export function tdm110Config(env: Env): Tdm110Config | null {
  const secret = (env.RDF_EDGE_SECRET || '').trim();
  if (secret.length < 24) return null;
  const pub = env.NEXT_PUBLIC_SPACETIME_HOST;
  const stdbHost = (env.RDF_SPACETIME_SERVER_HOST || (pub && pub !== 'same-origin' ? pub : '') || env.SPACETIME_HOST ||
    'https://maincloud.spacetimedb.com').replace(/\/+$/, '');
  const db = env.NEXT_PUBLIC_SPACETIME_DB_NAME || env.SPACETIME_DB_NAME || 'robo-factory-a706j';
  return { secret, stdbHost, db, stdbViaEdge: env.RDF_EDGE_SECRET_SPACETIME === '1' };
}

export type Tdm110State = {
  url: string | null; // verified orchestrator base
  candidate: string | null;
  lastId: number;
  checkedAt: number;
  provedAt: number;
  error: string | null;
};

/** Discovers and proves the orchestrator URL; one per server instance. */
export class Tdm110Resolver {
  state: Tdm110State = { url: null, candidate: null, lastId: 0, checkedAt: -Infinity, provedAt: 0, error: null };
  private inflight: Promise<string> | null = null;
  private cfg: Tdm110Config;
  private rawFetch: Fetch;
  private now: () => number;

  constructor(cfg: Tdm110Config, rawFetch: Fetch, now: () => number = Date.now) {
    this.cfg = cfg;
    this.rawFetch = rawFetch;
    this.now = now;
  }

  async resolve(): Promise<string> {
    const s = this.state;
    if (this.now() - s.checkedAt < TDM110_CACHE_MS) {
      if (s.url) return s.url;
      throw new Error(s.error || 'backend offline');
    }
    if (!this.inflight) {
      this.inflight = this.refresh().finally(() => {
        this.inflight = null;
      });
    }
    return this.inflight;
  }

  private async refresh(): Promise<string> {
    const s = this.state;
    try {
      const pick = await this.newest();
      if (pick) {
        s.lastId = pick.id;
        if (pick.url !== s.candidate) {
          s.candidate = pick.url;
          s.url = null;
          s.provedAt = 0;
        }
      }
      if (!s.candidate) {
        s.url = null;
        throw new Error(s.lastId ? 'backend offline (it published state=down; start make demo-hybrid)'
          : 'no backend published yet (start make demo-hybrid)');
      }
      if (!s.url || this.now() - s.provedAt > TDM110_REPROVE_MS) {
        await this.prove(s.candidate);
        s.url = s.candidate;
        s.provedAt = this.now();
      }
      s.error = null;
      return s.url;
    } catch (e) {
      s.url = null;
      s.provedAt = 0;
      s.error = `orchestrator discovery: ${(e as Error).message}`;
      throw new Error(s.error);
    } finally {
      s.checkedAt = this.now();
    }
  }

  private async newest() {
    const where = `type = '${TDM110_EVENT}'` + (this.state.lastId ? ` AND id > ${this.state.lastId}` : '');
    const headers: Record<string, string> = { 'content-type': 'text/plain' };
    if (this.cfg.stdbViaEdge) headers[TDM110_SECRET_HEADER] = this.cfg.secret;
    const res = await this.rawFetch(`${this.cfg.stdbHost}/v1/database/${this.cfg.db}/sql`, {
      method: 'POST',
      body: `SELECT * FROM event_log WHERE ${where}`,
      headers,
      cache: 'no-store',
      signal: AbortSignal.timeout(8_000),
    });
    if (!res.ok) throw new Error(`SpacetimeDB SQL ${res.status}`);
    return tdm110PickEndpoint(tdm110SqlRows(await res.json()));
  }

  private async prove(url: string): Promise<void> {
    const nonce = randomBytes(18).toString('base64url');
    let res: Response;
    try {
      res = await this.rawFetch(`${url}/_rdf/proof?nonce=${nonce}`, { cache: 'no-store', signal: AbortSignal.timeout(6_000) });
    } catch (e) {
      throw new Error(`backend not reachable at its published URL (${(e as Error).message})`);
    }
    const b = (await res.json().catch(() => null)) as { proof?: unknown } | null;
    if (!res.ok || typeof b?.proof !== 'string' || !sameHex(b.proof, tdm110Proof(this.cfg.secret, nonce, url))) {
      throw new Error(`the published URL failed the edge proof (HTTP ${res.status}); not sending it the secret`);
    }
  }
}

/** Which fetch targets the hook handles: 'discover' (rewrite to the discovered URL + secret), 'secret' (add the secret). */
export function tdm110Route(
  url: string,
  env: Env,
): { kind: 'discover'; rest: string } | { kind: 'secret' } | null {
  const explicit = (env.RDF_ORCHESTRATOR_URL || '').replace(/\/+$/, '');
  const discover = !explicit || explicit === TDM110_SENTINEL;
  if (discover) {
    const bases = [TDM110_SENTINEL];
    if (env.VERCEL) bases.push('http://localhost:8000'); // the routes' fallback when nothing is set
    const pubOrch = env.NEXT_PUBLIC_ORCHESTRATOR_URL || '';
    if (pubOrch.startsWith('/')) bases.push(pubOrch.replace(/\/+$/, '')); // relative: never valid server-side
    for (const b of bases) {
      if (url === b || url.startsWith(`${b}/`) || url.startsWith(`${b}?`)) return { kind: 'discover', rest: url.slice(b.length) };
    }
  } else if (url === explicit || url.startsWith(`${explicit}/`) || url.startsWith(`${explicit}?`)) {
    return { kind: 'secret' };
  }
  const c = tdm110Config(env);
  if (c?.stdbViaEdge && url.startsWith(`${c.stdbHost}/v1/`)) return { kind: 'secret' };
  return null;
}

type Tdm110Global = { __tdm110Hook?: { resolver: Tdm110Resolver | null } };

/** Wraps globalThis.fetch once per server instance (instrumentation.ts). No-op without RDF_EDGE_SECRET. */
export function tdm110InstallFetchHook(env: Env = process.env): boolean {
  const g = globalThis as unknown as Tdm110Global;
  if (g.__tdm110Hook) return true;
  const cfg = tdm110Config(env);
  if (!cfg) return false;
  if (!env.RDF_ORCHESTRATOR_URL) env.RDF_ORCHESTRATOR_URL = TDM110_SENTINEL; // routes read it at module load
  const raw: Fetch = globalThis.fetch.bind(globalThis);
  const resolver = new Tdm110Resolver(cfg, raw);
  g.__tdm110Hook = { resolver };
  const hooked = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const href = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
    const route = tdm110Route(href, env);
    if (!route) return raw(input, init);
    const target = route.kind === 'discover' ? `${await resolver.resolve()}${route.rest}` : href;
    if (typeof input === 'string' || input instanceof URL) {
      const headers = new Headers(init?.headers);
      headers.set(TDM110_SECRET_HEADER, cfg.secret);
      return raw(target, { ...init, headers });
    }
    const req = new Request(target, input);
    req.headers.set(TDM110_SECRET_HEADER, cfg.secret);
    return raw(req, init);
  };
  globalThis.fetch = hooked as Fetch;
  return true;
}

/** The instance's resolver state (for /api/tdm110/backend), or null when the hook is off. */
export function tdm110Resolver(): Tdm110Resolver | null {
  return (globalThis as unknown as Tdm110Global).__tdm110Hook?.resolver ?? null;
}
