// TDM-59: tiny no-cache endpoint the practice-mode readiness check times to measure the
// operator's own network round trip. Returns the server clock; carries no data.

export const dynamic = 'force-dynamic';

export function GET() {
  return new Response(JSON.stringify({ t: Date.now() }), {
    headers: { 'content-type': 'application/json', 'cache-control': 'no-store, max-age=0' },
  });
}
