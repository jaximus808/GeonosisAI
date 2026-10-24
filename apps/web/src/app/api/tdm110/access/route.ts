// TDM-110: the access code on the web app itself (Vercel), served at /_rdf/access (next.config.ts rewrite) so
// /join's AccessCodeForm and the dashboard's JoinQr work unchanged. Same answers as the make demo-public edge:
//   GET  -> {required, ok, public_url, code?}   code only for a logged-in operator (shown under the QR)
//   POST {code} -> sets the HttpOnly rdf_access cookie (7 days); 401 on a wrong code; 429 when throttled
// The code is RDF_PUBLIC_ACCESS_CODE (server env; tdm110GateCode). Unset = no gate (required: false).

import {
  TDM110_ACCESS_COOKIE,
  TDM110_ACCESS_TTL_S,
  tdm110CodeMatches,
  tdm110GateCode,
  tdm110MintAccess,
  tdm110VerifyAccess,
} from '@/lib/tdm110/access';
import { operatorSessionOf, readCookie } from '@/lib/operator/session';

export const dynamic = 'force-dynamic';
const NO_STORE = { 'cache-control': 'no-store' };

function accessCode(): string {
  return tdm110GateCode(process.env);
}

function publicUrl(): string | null {
  return (process.env.RDF_WEB_PUBLIC_URL || process.env.NEXT_PUBLIC_WEB_PUBLIC_URL || '').replace(/\/+$/, '') || null;
}

// Per-instance throttle on wrong codes (a serverless instance is short-lived; this stops scripts, not people).
const tdm110Fails = new Map<string, { n: number; at: number }>();
function clientIp(req: Request): string {
  return (req.headers.get('x-forwarded-for') || '').split(',')[0].trim() || 'unknown';
}
function throttled(req: Request): boolean {
  const f = tdm110Fails.get(clientIp(req));
  return Boolean(f && Date.now() - f.at < 600_000 && f.n >= 30);
}
function noteFail(req: Request) {
  const ip = clientIp(req);
  const f = tdm110Fails.get(ip);
  tdm110Fails.set(ip, f && Date.now() - f.at < 600_000 ? { n: f.n + 1, at: f.at } : { n: 1, at: Date.now() });
  if (tdm110Fails.size > 10_000) tdm110Fails.clear();
}

function isHttps(req: Request): boolean {
  const fwd = req.headers.get('x-forwarded-proto')?.split(',')[0]?.trim().toLowerCase();
  if (fwd) return fwd === 'https';
  return new URL(req.url).protocol === 'https:';
}

export function GET(req: Request) {
  const code = accessCode();
  const ok = !code || tdm110VerifyAccess(code, readCookie(req, TDM110_ACCESS_COOKIE));
  const body: Record<string, unknown> = { required: Boolean(code), ok, public_url: publicUrl() };
  if (code && operatorSessionOf(req) != null) body.code = code;
  return Response.json(body, { headers: NO_STORE });
}

export async function POST(req: Request) {
  const code = accessCode();
  if (!code) return Response.json({ ok: true, required: false }, { headers: NO_STORE });
  if (throttled(req)) {
    return Response.json({ code: 'rate_limited', detail: 'Too many tries. Wait 10 minutes.' }, { status: 429, headers: NO_STORE });
  }
  const b = (await req.json().catch(() => null)) as { code?: unknown } | null;
  if (!tdm110CodeMatches(code, b?.code)) {
    noteFail(req);
    return Response.json(
      { code: 'bad_code', detail: 'That code is not right. It is 6 characters, on the screen next to the QR code.' },
      { status: 401, headers: NO_STORE },
    );
  }
  const cookie = [
    `${TDM110_ACCESS_COOKIE}=${tdm110MintAccess(code)}`,
    'Path=/',
    `Max-Age=${TDM110_ACCESS_TTL_S}`,
    'HttpOnly',
    'SameSite=Lax',
    ...(isHttps(req) ? ['Secure'] : []),
  ].join('; ');
  return Response.json({ ok: true }, { headers: { ...NO_STORE, 'set-cookie': cookie } });
}
