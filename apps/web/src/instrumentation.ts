// TDM-79: fail fast on a bad web config. Next calls register() once per server start (dev, build's
// server, start), before any request. loadEnv prints the missing/invalid var names (never values)
// and exits(2), the same contract as the other apps (@rdf/env).
//
// The web app reads the browser-safe NEXT_PUBLIC_* names, so they are mapped onto webEnv's keys here;
// the repo-root .env fills anything unset. Only webEnv's keys are returned (zod strips the rest),
// and nothing here reaches the browser.
export async function register() {
  if (process.env.NEXT_RUNTIME !== 'nodejs') return;
  // TDM-110 hybrid deploy (Vercel): with RDF_EDGE_SECRET set, server-side fetches to the orchestrator go to the
  // URL the backend published in SpacetimeDB (after an HMAC proof) and carry the edge secret. No-op without it.
  const { tdm110InstallFetchHook } = await import('@/lib/tdm110/edge');
  tdm110InstallFetchHook();
  const { loadEnv, webEnv } = await import('@rdf/env');
  const e = process.env;
  loadEnv(webEnv, 'web', {
    env: {
      ...e,
      SPACETIME_HOST:
        e.RDF_SPACETIME_SERVER_HOST ||
        (e.NEXT_PUBLIC_SPACETIME_HOST && e.NEXT_PUBLIC_SPACETIME_HOST !== 'same-origin' ? e.NEXT_PUBLIC_SPACETIME_HOST : undefined) ||
        e.SPACETIME_HOST,
      SPACETIME_DB_NAME: e.NEXT_PUBLIC_SPACETIME_DB_NAME || e.SPACETIME_DB_NAME,
      ORCHESTRATOR_URL: e.RDF_ORCHESTRATOR_URL || e.NEXT_PUBLIC_ORCHESTRATOR_URL || e.ORCHESTRATOR_URL,
    },
  });
}
