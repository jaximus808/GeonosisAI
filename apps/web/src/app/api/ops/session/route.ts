// Operator session for the web app (see lib/operator/session.ts).
//
//   GET    /api/ops/session   -> {configured, operator, expiresAt}   (the cookie is HttpOnly, so pages ask here)
//   POST   /api/ops/session   {passcode} -> sets the rdf_op cookie (12 h); 401 on a wrong passcode
//   DELETE /api/ops/session   -> clears it
//
// The passcode is never logged or echoed.

import {
  checkPasscode,
  clearLoginFailures,
  clearOperatorCookieHeader,
  loginThrottled,
  mintOperatorSession,
  noteLoginFailure,
  operatorConfigured,
  operatorCookieHeader,
  operatorSessionOf,
} from '@/lib/operator/session';

const NO_STORE = { 'cache-control': 'no-store' };

export const dynamic = 'force-dynamic';

export function GET(req: Request) {
  const exp = operatorSessionOf(req);
  return Response.json({ configured: operatorConfigured(), operator: exp != null, expiresAt: exp }, { headers: NO_STORE });
}

export async function POST(req: Request) {
  if (!operatorConfigured()) {
    return Response.json(
      { ok: false, code: 'operator_login_unconfigured', detail: 'Operator login is not set up on this server (RDF_OPERATOR_PASSCODE).' },
      { status: 503, headers: NO_STORE },
    );
  }
  if (loginThrottled(req)) {
    return Response.json({ ok: false, code: 'too_many_attempts', detail: 'Too many wrong passcodes. Wait 10 minutes.' }, { status: 429, headers: NO_STORE });
  }
  const b = (await req.json().catch(() => null)) as { passcode?: unknown } | null;
  if (!checkPasscode(b?.passcode)) {
    noteLoginFailure(req);
    return Response.json({ ok: false, code: 'bad_passcode', detail: 'Wrong passcode.' }, { status: 401, headers: NO_STORE });
  }
  clearLoginFailures(req);
  const s = mintOperatorSession();
  return Response.json(
    { ok: true, operator: true, expiresAt: s.expiresAt },
    { headers: { ...NO_STORE, 'set-cookie': operatorCookieHeader(req, s.value) } },
  );
}

export function DELETE(req: Request) {
  return Response.json({ ok: true, operator: false }, { headers: { ...NO_STORE, 'set-cookie': clearOperatorCookieHeader(req) } });
}
