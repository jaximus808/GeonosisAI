// TDM-110: access-code gate for the web app when nothing sits in front of it (the Vercel deploy). Off unless
// RDF_WEB_ACCESS_CODE is set (server env), so `next dev`, make demo and make demo-public (whose edge gates
// with RDF_PUBLIC_ACCESS_CODE before Next ever sees the request) behave exactly as before.
// Same contract as scripts/demo/public_guard.py: pages redirect to /join?next=..., API calls get 401, and
// /join learns whether to show the code form from `x-rdf-access` (any client-sent value is dropped).
// The code form posts to /_rdf/access, rewritten to /api/tdm110/access (next.config.ts).

import { NextResponse, type NextRequest } from 'next/server';
import { TDM110_ACCESS_COOKIE, tdm110AccessDecision } from '@/lib/tdm110/access';

export function proxy(req: NextRequest) {
  const code = (process.env.RDF_WEB_ACCESS_CODE || '').trim();
  if (!code) return NextResponse.next();
  const d = tdm110AccessDecision(code, {
    path: req.nextUrl.pathname,
    search: req.nextUrl.search,
    method: req.method,
    accept: req.headers.get('accept') ?? '',
    cookie: req.cookies.get(TDM110_ACCESS_COOKIE)?.value,
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
