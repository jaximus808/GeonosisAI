// TDM-110: the make demo-public access code, enforced by the web app itself (Vercel has no local edge).
// Same cookie as scripts/demo/public_guard.py (`rdf_access` = "v1.<exp>.<sig>", sig = base64url HMAC-SHA256
// over "v1.<exp>" with key HMAC-SHA256(normalized code, "rdf-public-access-v1"), 7 days), so either side can
// verify the other's cookie. The code is RDF_PUBLIC_ACCESS_CODE (server env, never NEXT_PUBLIC_), the one
// variable the edge, the job drops and Vercel share; the web app gates only on Vercel or with RDF_WEB_GATE=1
// (behind make demo-public's edge the edge already gated). Pure apart from node:crypto (test/tdm110-hybrid.test.ts).

import { createHash, createHmac, timingSafeEqual } from 'node:crypto';

export const TDM110_ACCESS_COOKIE = 'rdf_access';
export const TDM110_ACCESS_TTL_S = 7 * 24 * 3600;
const LABEL = 'rdf-public-access-v1';

/** The code this web server enforces itself ('' = no gate). */
export function tdm110GateCode(env: Record<string, string | undefined>): string {
  const on = Boolean(env.VERCEL) || env.RDF_WEB_GATE === '1';
  return on ? (env.RDF_PUBLIC_ACCESS_CODE || '').trim() : '';
}

export function tdm110NormalizeCode(code: string): string {
  return (code || '').replace(/[\s\-_.]/g, '').toUpperCase();
}

function key(code: string): Buffer {
  return createHmac('sha256', tdm110NormalizeCode(code)).update(LABEL).digest();
}

export function tdm110MintAccess(code: string, nowS = Math.floor(Date.now() / 1000), ttlS = TDM110_ACCESS_TTL_S): string {
  const body = `v1.${Math.floor(nowS + ttlS)}`;
  return `${body}.${createHmac('sha256', key(code)).update(body).digest('base64url')}`;
}

export function tdm110VerifyAccess(code: string, value: string | undefined | null, nowS = Math.floor(Date.now() / 1000)): boolean {
  if (!code || !value) return false;
  const parts = value.split('.');
  if (parts.length !== 3 || parts[0] !== 'v1' || !/^\d{1,12}$/.test(parts[1])) return false;
  const exp = Number(parts[1]);
  if (exp <= nowS || exp > nowS + TDM110_ACCESS_TTL_S + 60) return false;
  const want = Buffer.from(tdm110MintAccess(code, exp, 0));
  const got = Buffer.from(value);
  return want.length === got.length && timingSafeEqual(want, got);
}

export function tdm110CodeMatches(code: string, candidate: unknown): boolean {
  if (!code || typeof candidate !== 'string') return false;
  const a = createHash('sha256').update(tdm110NormalizeCode(candidate)).digest();
  const b = createHash('sha256').update(tdm110NormalizeCode(code)).digest();
  return timingSafeEqual(a, b);
}

/** Paths a phone without the cookie may still open (the code form and what it needs to render). */
export function tdm110AccessOpen(path: string): boolean {
  return (
    path === '/join' ||
    path === '/_rdf/access' ||
    path === '/api/tdm110/access' ||
    path === '/api/tdm110/backend' ||
    /^\/(_next\/static\/|_next\/image|favicon\.ico$|icon|apple-icon|manifest\.webmanifest$)/.test(path)
  );
}

/** What the gate does with a request: pass (with the x-rdf-access value to forward), redirect, or 401. */
export function tdm110AccessDecision(
  code: string,
  req: { path: string; search: string; method: string; accept: string; cookie: string | undefined },
): { action: 'pass'; access: 'off' | 'ok' | 'required' } | { action: 'redirect'; to: string } | { action: 'deny' } {
  if (!code) return { action: 'pass', access: 'off' };
  if (tdm110VerifyAccess(code, req.cookie)) return { action: 'pass', access: 'ok' };
  if (tdm110AccessOpen(req.path)) return { action: 'pass', access: 'required' };
  if ((req.method === 'GET' || req.method === 'HEAD') && !req.path.startsWith('/api/') && req.accept.includes('text/html')) {
    return { action: 'redirect', to: `/join?next=${encodeURIComponent(req.path + req.search).replace(/%2F/g, '/')}` };
  }
  return { action: 'deny' };
}
