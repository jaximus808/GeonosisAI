// Operator login for the web app (server only: reads RDF_OPERATOR_PASSCODE, never sent to the browser).
//
// The web server's proxy routes add the orchestrator's operator key (RDF_OPERATOR_KEY) on their own,
// so without this anyone who can open the site could move the arm. Every route that moves the robot
// or does another operator-only thing (create a ticket, change run settings / budget, save a report,
// revoke a control lease) calls requireOperator() first. The soft e-stop route never does: stopping
// is always allowed.
//
// Session cookie `rdf_op` = "v1.<exp unix s>.<nonce>.<sig>", sig = HMAC-SHA256 over "v1.<exp>.<nonce>"
// with a key derived from the passcode. HttpOnly, SameSite=Strict, Secure when the request came over
// https (directly or via the tunnel's X-Forwarded-Proto), Max-Age 12 h. Changing the passcode logs
// every session out. No passcode configured = every guarded route refuses (fail closed).

import { createHash, createHmac, randomBytes, timingSafeEqual } from 'node:crypto';
import { tdm78Env } from '@/lib/tdm78/server';

export const OPERATOR_COOKIE = 'rdf_op';
export const OPERATOR_SESSION_S = 12 * 60 * 60;
const KEY_LABEL = 'rdf-operator-session-v1';

function passcode(): string | undefined {
  return tdm78Env('RDF_OPERATOR_PASSCODE');
}

export function operatorConfigured(): boolean {
  return Boolean(passcode());
}

function sigFor(pass: string, body: string): Buffer {
  const key = createHmac('sha256', pass).update(KEY_LABEL).digest();
  return createHmac('sha256', key).update(body).digest();
}

/** A fresh signed session value, valid for OPERATOR_SESSION_S. */
export function mintOperatorSession(nowMs = Date.now()): { value: string; expiresAt: number } {
  const pass = passcode();
  if (!pass) throw new Error('RDF_OPERATOR_PASSCODE is not set');
  const exp = Math.floor(nowMs / 1000) + OPERATOR_SESSION_S;
  const body = `v1.${exp}.${randomBytes(12).toString('base64url')}`;
  return { value: `${body}.${sigFor(pass, body).toString('base64url')}`, expiresAt: exp * 1000 };
}

/** Expiry (ms) of a valid session value, else null. */
export function verifyOperatorSession(value: string | undefined | null, nowMs = Date.now()): number | null {
  const pass = passcode();
  if (!pass || !value) return null;
  const parts = value.split('.');
  if (parts.length !== 4 || parts[0] !== 'v1' || !/^\d{1,12}$/.test(parts[1])) return null;
  const exp = Number(parts[1]);
  const now = Math.floor(nowMs / 1000);
  if (exp <= now || exp > now + OPERATOR_SESSION_S + 60) return null;
  const want = sigFor(pass, parts.slice(0, 3).join('.'));
  let got: Buffer;
  try {
    got = Buffer.from(parts[3], 'base64url');
  } catch {
    return null;
  }
  if (got.length !== want.length || !timingSafeEqual(got, want)) return null;
  return exp * 1000;
}

/** Constant-time passcode check. */
export function checkPasscode(candidate: unknown): boolean {
  const pass = passcode();
  if (!pass || typeof candidate !== 'string' || !candidate) return false;
  const a = createHash('sha256').update(candidate).digest();
  const b = createHash('sha256').update(pass).digest();
  return timingSafeEqual(a, b);
}

export function readCookie(req: Request, name: string): string | undefined {
  const raw = req.headers.get('cookie');
  if (!raw) return undefined;
  for (const part of raw.split(';')) {
    const i = part.indexOf('=');
    if (i > 0 && part.slice(0, i).trim() === name) return part.slice(i + 1).trim();
  }
  return undefined;
}

/** Expiry (ms) when this request carries a valid operator session, else null. */
export function operatorSessionOf(req: Request): number | null {
  return verifyOperatorSession(readCookie(req, OPERATOR_COOKIE));
}

function isHttps(req: Request): boolean {
  const fwd = req.headers.get('x-forwarded-proto')?.split(',')[0]?.trim().toLowerCase();
  if (fwd) return fwd === 'https';
  try {
    return new URL(req.url).protocol === 'https:';
  } catch {
    return false;
  }
}

export function operatorCookieHeader(req: Request, value: string, maxAgeS = OPERATOR_SESSION_S): string {
  return [
    `${OPERATOR_COOKIE}=${value}`,
    'Path=/',
    `Max-Age=${maxAgeS}`,
    'HttpOnly',
    'SameSite=Strict',
    ...(isHttps(req) ? ['Secure'] : []),
  ].join('; ');
}

export function clearOperatorCookieHeader(req: Request): string {
  return operatorCookieHeader(req, '', 0);
}

/**
 * Guard for operator-only proxy routes: null when the caller is a logged-in operator, else the
 * refusal to return. `detail` is a plain string so every existing caller shows it as-is.
 */
export function requireOperator(req: Request): Response | null {
  const headers = { 'cache-control': 'no-store' };
  if (!operatorConfigured()) {
    return Response.json(
      {
        ok: false,
        code: 'operator_login_unconfigured',
        detail: 'Operator login is not set up on this server (RDF_OPERATOR_PASSCODE). The soft e-stop still works.',
      },
      { status: 503, headers },
    );
  }
  if (operatorSessionOf(req) != null) return null;
  return Response.json(
    { ok: false, code: 'operator_login_required', detail: 'Operator login required: log in at /ops/login.' },
    { status: 401, headers },
  );
}

// --- login throttle (in memory, per client address): the passcode is long, this just stops hammering.
const FAILS = new Map<string, { n: number; until: number }>();
const MAX_FAILS = 8;
const WINDOW_MS = 10 * 60 * 1000;

function clientKey(req: Request): string {
  return (
    req.headers.get('cf-connecting-ip') ||
    req.headers.get('x-forwarded-for')?.split(',')[0]?.trim() ||
    req.headers.get('x-real-ip') ||
    'local'
  );
}

export function loginThrottled(req: Request, nowMs = Date.now()): boolean {
  const f = FAILS.get(clientKey(req));
  return Boolean(f && f.until > nowMs && f.n >= MAX_FAILS);
}

export function noteLoginFailure(req: Request, nowMs = Date.now()): void {
  const k = clientKey(req);
  const f = FAILS.get(k);
  if (!f || f.until <= nowMs) FAILS.set(k, { n: 1, until: nowMs + WINDOW_MS });
  else f.n += 1;
}

export function clearLoginFailures(req: Request): void {
  FAILS.delete(clientKey(req));
}
