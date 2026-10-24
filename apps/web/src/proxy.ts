// TDM-110: access-code gate for the web app when nothing sits in front of it (the Vercel deploy). The code is
// RDF_PUBLIC_ACCESS_CODE, the same variable make demo-public's edge and the job drops (Discord posts) use, so
// one code works everywhere. It gates only on Vercel (VERCEL is set) or with RDF_WEB_GATE=1, so `next dev`,
// make demo and make demo-public (whose edge gates before Next ever sees the request) behave as before.
// Same contract as scripts/demo/public_guard.py: pages redirect to /join?next=..., API calls get 401, and
// /join learns whether to show the code form from `x-rdf-access` (any client-sent value is dropped).
// The code form posts to /_rdf/access, rewritten to /api/tdm110/access (next.config.ts).

import { NextResponse, type NextRequest } from 'next/server';
import { TDM110_ACCESS_COOKIE, tdm110AccessDecision, tdm110GateCode } from '@/lib/tdm110/access';
import { OPERATOR_COOKIE, verifyOperatorSession } from '@/lib/operator/session';
import { TDM108_TASK_COOKIE, tdm108InviteSecret, tdm108TaskScopeAllows, tdm108VerifyTaskCookie } from '@/lib/tdm108/invite';

export function proxy(req: NextRequest) {
  const code = tdm110GateCode(process.env);
  if (!code) return NextResponse.next();
  const scope = tdm108VerifyTaskCookie(tdm108InviteSecret(process.env), req.cookies.get(TDM108_TASK_COOKIE)?.value);
  const d = tdm110AccessDecision(code, {
    path: req.nextUrl.pathname,
    search: req.nextUrl.search,
    method: req.method,
    accept: req.headers.get('accept') ?? '',
    cookie: req.cookies.get(TDM110_ACCESS_COOKIE)?.value,
    // TDM-111: a logged-in operator never needs the contributor code
    operator: verifyOperatorSession(req.cookies.get(OPERATOR_COOKIE)?.value) != null,
    // TDM-108: a job-drop invite opens its own task only
    task: scope && {
      ids: scope.ids,
      allows: tdm108TaskScopeAllows(scope, req.method, req.nextUrl.pathname, req.nextUrl.search),
    },
  });
  if (d.action === 'redirect') return NextResponse.redirect(new URL(d.to, req.url));
  if (d.action === 'deny') {
    return NextResponse.json(
      { code: 'access_code_required', detail: 'Enter the access code at /join first.' },
      { status: 401, headers: { 'cache-control': 'no-store' } },
    );
  }
  const headers = new Headers(req.headers);
  headers.set('x-rdf-access', d.access);
  return NextResponse.next({ request: { headers } });
}

export const config = {
  matcher: ['/((?!_next/static|_next/image|favicon.ico).*)'],
};
