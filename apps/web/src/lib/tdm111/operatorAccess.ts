// TDM-111: a successful operator login also sets the contributor access cookie (rdf_access, same format as
// TDM-110 / scripts/demo/public_guard.py), so the access gate never sends an operator to /join. Uses
// RDF_PUBLIC_ACCESS_CODE whenever it is set, gate on or off: behind make demo-public's edge the web app does
// not gate itself, and the edge mints the same cookie on a 200 from POST /api/ops/session anyway.
// Pure apart from node:crypto (test/tdm111-landing.test.ts).

import { TDM110_ACCESS_COOKIE, TDM110_ACCESS_TTL_S, tdm110MintAccess } from '@/lib/tdm110/access';

function isHttps(req: Request): boolean {
  const fwd = req.headers.get('x-forwarded-proto')?.split(',')[0]?.trim().toLowerCase();
  if (fwd) return fwd === 'https';
  try {
    return new URL(req.url).protocol === 'https:';
  } catch {
    return false;
  }
}

/** The Set-Cookie value for rdf_access, or null when no access code is configured. */
export function tdm111AccessCookieForOperator(req: Request, env: Record<string, string | undefined>, nowS?: number): string | null {
  const code = (env.RDF_PUBLIC_ACCESS_CODE || '').trim();
  if (!code) return null;
  return [
    `${TDM110_ACCESS_COOKIE}=${tdm110MintAccess(code, nowS)}`,
    'Path=/',
    `Max-Age=${TDM110_ACCESS_TTL_S}`,
    'HttpOnly',
    'SameSite=Lax',
    ...(isHttps(req) ? ['Secure'] : []),
  ].join('; ');
}
